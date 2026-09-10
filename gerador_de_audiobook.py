import os
import re
import asyncio
import shutil
import zipfile
import gc
import time
import logging
from tqdm import tqdm
from colorama import init, Fore, Style
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from pypdf import PdfReader
import mobi
from deep_translator import GoogleTranslator
from langdetect import detect, DetectorFactory
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TALB, TRCK, TPE1, CHAP, CTOC
import tkinter as tk
from tkinter import filedialog
import json
import urllib.request

# --- CONFIGURAÇÕES DE AMBIENTE ---
logging.basicConfig(
    filename='audiobook_generator.log',
    filemode='a',
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    level=logging.INFO,
    encoding='utf-8'
)

DetectorFactory.seed = 0
init(autoreset=True)

PASTA_ORIGEM = 'arquivos'
PASTA_DESTINO = 'meus audiobooks'
PASTA_DRIVE = r"G:\Meu Drive\Audiobooks"

# --- TELEMETRIA: BAR_FORMAT COM {remaining} ---
F_LOTE = "{desc} {percentage:3.0f}% |{bar}| {n_fmt}/{total_fmt} livros [Tempo: {elapsed}<{remaining}]"
F_LIVRO = "{desc} {percentage:3.0f}% |{bar}| {n_fmt}/{total_fmt} partes [Restam: {remaining} | Vel: {rate_fmt}]"
F_CAP = "{desc} {percentage:3.0f}% |{bar}| {n_fmt}/{total_fmt} partes"

# Dicionários NLP para Detecção de Capítulos
UNIDADES = 'UM|DOIS|TRÊS|QUATRO|CINCO|SEIS|SETE|OITO|NOVE'
DEZENAS = 'DEZ|VINTE|TRINTA|QUARENTA|CINQUENTA|SESSENTA|SETENTA|OITENTA|NOVENTA'
CENTENAS = 'CEM|CENTO|DUZENTOS|TREZENTOS|QUATROCENTOS|QUINHENTOS|SEISCENTOS|SETECENTOS|OITOCENTOS|NOVECENTOS'
ESPECIAIS = 'ONZE|DOZE|TREZE|CATORZE|QUINZE|DEZESSEIS|DEZESSETE|DEZOITO|DEZENOVE'
ORDINAIS = 'PRIMEIRO|SEGUNDO|TERCEIRO|QUARTO|QUINTO|SEXTO|SÉTIMO|SETIMO|OITAVO|NONO|DÉCIMO|DECIMO|VIGÉSIMO|VIGESIMO|TRIGÉSIMO|TRIGESIMO|QUADRAGÉSIMO|QUADRAGESIMO|QUINQUAGÉSIMO|QUINQUAGESIMO|SEXAGÉSIMO|SEXAGESIMO|SEPTUAGÉSIMO|SEPTUAGESIMO|OCTOGÉSIMO|OCTOGESIMO|NONAGÉSIMO|NONAGESIMO|CENTÉSIMO|CENTESIMO'
EXTENSO_COMPLETO = rf'(?:(?:{CENTENAS})|(?:{DEZENAS})(?:\s+E\s+(?:{UNIDADES}))?|{UNIDADES}|{ESPECIAIS}|{ORDINAIS})'
PALAVRAS_CHAVE = ['PRÓLOGO', 'PROLOGO', 'EPÍLOGO', 'EPILOGO', 'INTRODUÇÃO', 'CAPÍTULO', 'CAPITULO', 'PREFÁCIO', 'PARTE', 'LIVRO']

VOZES_MAP = {
    "1": ("pt-BR-AntonioNeural", "Antonio (Português BR - Masculino)"),
    "2": ("pt-BR-FranciscaNeural", "Francisca (Português BR - Feminino)"),
    "3": ("en-US-GuyNeural", "Guy (Inglês US - Masculino)"),
    "4": ("en-US-AvaNeural", "Ava (Inglês US - Feminino)"),
    "5": ("es-ES-AlvaroNeural", "Alvaro (Espanhol ES - Masculino)"),
    "6": ("es-ES-ElviraNeural", "Elvira (Espanhol ES - Feminino)"),
    "7": ("ar-SA-HamedNeural", "Hamed (Árabe SA - Masculino)"),
    "8": ("ar-SA-ZariyahNeural", "Zariyah (Árabe SA - Feminino)")
}

# --- FUNÇÕES AUXILIARES ---
def otimizar_texto_ptbr(texto):
    subs = {
        r'\bSr\.\s': 'Senhor ', r'\bSrs\.\s': 'Senhores ',
        r'\bSra\.\s': 'Senhora ', r'\bSras\.\s': 'Senhoras ',
        r'\bDr\.\s': 'Doutor ', r'\bDra\.\s': 'Doutora ',
        r'\bProf\.\s': 'Professor ', r'\bProfa\.\s': 'Professora ',
        r'\bCap\.\s': 'Capítulo ', r'\bpág\.\s': 'página ',
        r'\b(etc)\.': 'e cetera.', r'&': ' e ',
    }
    for padrao, substituto in subs.items():
        texto = re.sub(padrao, substituto, texto, flags=re.IGNORECASE)
    # Remove caracteres problemáticos
    texto = re.sub(r'[~#\^_{}\[\]\|\\]', ' ', texto)
    # Padroniza travessões
    texto = re.sub(r'[-–—]', '—', texto)
    # Espaços duplos
    texto = re.sub(r' {2,}', ' ', texto)
    return texto.strip()

def formatar_tempo_hms(segundos):
    s = int(segundos)
    h = s // 3600
    m = (s % 3600) // 60
    seg = s % 60
    if h > 0: return f"{h}h {m}m {seg}s"
    if m > 0: return f"{m}m {seg}s"
    return f"{seg}s"

# --- CAMADA DE EXTRAÇÃO (E) ---
def extrair_texto(caminho):
    ext = caminho.lower().split('.')[-1]
    try:
        if ext == 'txt':
            with open(caminho, 'r', encoding='utf-8') as f: return f.read()
        elif ext == 'epub':
            try:
                book = epub.read_epub(caminho)
                return "\n\n".join([BeautifulSoup(i.get_content(), 'html.parser').get_text(separator='\n\n', strip=True) 
                                    for i in book.get_items() if i.get_type() == ebooklib.ITEM_DOCUMENT])
            except:
                texto_fallback = []
                with zipfile.ZipFile(caminho, 'r') as z:
                    for item in z.namelist():
                        if item.endswith(('.html', '.xhtml', '.htm')):
                            soup = BeautifulSoup(z.read(item), 'html.parser')
                            texto_fallback.append(soup.get_text(separator='\n\n', strip=True))
                return "\n\n".join(texto_fallback)
        elif ext == 'pdf':
            return "\n\n".join([p.extract_text() for p in PdfReader(caminho).pages if p.extract_text()])
        elif ext in ['mobi', 'azw']:
            temp_dir, html_content = mobi.extract(caminho)
            with open(html_content, 'r', encoding='utf-8') as f:
                texto = BeautifulSoup(f.read(), 'html.parser').get_text(separator='\n\n', strip=True)
            if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
            return texto
    except Exception as e:
        logging.error(f"Erro na extração de {caminho}: {e}", exc_info=True)
        print(f"{Fore.RED}❌ Erro na extração: {e}")
        return None

# --- CAMADA DE TRADUÇÃO (T) ---
def traduzir_gemini(bloco, contexto, api_key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    prompt = "Você é um tradutor literário profissional. Traduza o texto a seguir para Português do Brasil (PT-BR), mantendo o estilo e formatação originais. Retorne APENAS o texto traduzido."
    if contexto:
        prompt += f"\n\nContexto da história anterior (NÃO TRADUZA ISTO, apenas use para entender a cena):\n{contexto}"
    prompt += f"\n\nTexto a traduzir:\n{bloco}"

    data = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.3}}
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as response:
        res_json = json.loads(response.read().decode('utf-8'))
        return res_json['candidates'][0]['content']['parts'][0]['text'].strip()

async def traduzir_bloco(idx, bloco, contexto, motor, api_key, semaforo, pb_local):
    async with semaforo:
        espera = 3
        for tentativa in range(4):
            try:
                if motor == "gemini":
                    res = await asyncio.to_thread(traduzir_gemini, bloco, contexto, api_key)
                else:
                    tradutor = GoogleTranslator(source='auto', target='pt')
                    res = await asyncio.to_thread(tradutor.translate, bloco)
                
                if not res or not res.strip():
                    raise ValueError("Tradução vazia retornada.")
                
                if motor == "google" and len(bloco) > 100 and len(res) < len(bloco) * 0.3:
                    raise ValueError("Tradução muito curta.")
                
                pb_local.update(1)
                return idx, res
            except Exception as e:
                logging.warning(f"Tentativa {tentativa+1}/4 falhou no bloco {idx}: {e}")
                await asyncio.sleep(espera)
                espera *= 2
                
        logging.error(f"Falha definitiva na tradução do bloco {idx}. Mantendo original.")
        print(f"\n{Fore.RED}⚠️ Falha na tradução do bloco {idx}. Mantendo original.{Style.RESET_ALL}")
        pb_local.update(1)
        return idx, bloco

async def traduzir_texto(texto_bruto, nome_arq, pos_ui, motor="google", api_key=""):
    paragrafos = texto_bruto.split('\n\n')
    blocos, bloco_atual, tam = [], [], 0
    for p in paragrafos:
        if tam + len(p) < 3000:
            bloco_atual.append(p)
            tam += len(p) + 2
        else:
            if bloco_atual: blocos.append("\n\n".join(bloco_atual))
            bloco_atual, tam = [p], len(p)
    if bloco_atual: blocos.append("\n\n".join(bloco_atual))

    semaforo = asyncio.Semaphore(5)
    with tqdm(total=len(blocos), desc=f"{Fore.YELLOW}🌍 Trad. {nome_arq[:10]}", position=pos_ui, leave=False, colour="yellow", bar_format=F_LIVRO) as pb_l:
        tasks = []
        for i, b in enumerate(blocos):
            contexto = blocos[i-1][-1500:] if i > 0 else ""
            tasks.append(traduzir_bloco(i, b, contexto, motor, api_key, semaforo, pb_l))
        resultados = await asyncio.gather(*tasks)
    
    resultados.sort(key=lambda x: x[0])
    return "\n\n".join([r[1] for r in resultados])

# --- CAMADA DE NLP ---
def identificar_marcos(linhas, idx, modo_romano):
    linha = linhas[idx].strip()
    if not linha: return False, None, 0
    up = linha.upper()
    for pw in PALAVRAS_CHAVE:
        if up.startswith(pw):
            reg = rf'^({pw}(?:\s+(?:\b(?:{EXTENSO_COMPLETO})\b|\d+))?)(.*)$'
            m = re.match(reg, up, re.IGNORECASE)
            if not m and modo_romano:
                m = re.match(rf'^({pw}(?:\s+\b[IVXLCDM]+\b)?)(.*)$', up, re.IGNORECASE)
            if m:
                tit, resto = m.group(1).strip(), m.group(len(m.groups())).strip()
                if not resto and idx + 1 < len(linhas):
                    prox = linhas[idx+1].strip()
                    if 0 < len(prox) < 45 and not prox.endswith(('.', '!', '?', ':')): 
                        return True, f"{tit} {prox}".title(), 2
                return True, tit.title(), 1
    if re.match(r'^(\d+|[IVXLCDM]+)\.?\s*$', up): return True, f"Capítulo {up.replace('.', '')}", 1
    return False, None, 0

def estruturar_obra_nlp(texto_bruto, eh_traducao, pos_ui):
    linhas_raw = texto_bruto.split('\n')
    contagem = {}
    for l in linhas_raw:
        if len(l.strip()) > 15: contagem[l.strip()] = contagem.get(l.strip(), 0) + 1
    lixo = {l for l, c in contagem.items() if c > 3}
    linhas = [l.strip() for l in linhas_raw if l.strip() not in lixo and l.strip()]
    
    score_romano = sum(5 for l in linhas[:1000] if re.search(r'\bCAP[ÍI]TULO\s+[IVXLC]+\b', l.upper()))
    modo_romano = score_romano >= 10
    capitulos = []
    cap_atual = {"titulo": "00 - Introducao", "conteudo": []}
    
    i = 0
    with tqdm(total=len(linhas), desc=f"{Fore.CYAN}⚙️ NLP Engine", position=pos_ui, leave=False, colour="cyan", bar_format=F_LIVRO) as pb:
        while i < len(linhas):
            is_cap, tit, cons = identificar_marcos(linhas, i, modo_romano)
            if is_cap:
                if cap_atual["conteudo"]:
                    cap_atual["conteudo"] = "".join(cap_atual["conteudo"]).strip()
                    capitulos.append(cap_atual)
                cap_atual = {"titulo": f"{len(capitulos):02d} - {tit}", "conteudo": []}
                i += cons
                pb.update(cons)
            else:
                p = re.sub(r'([a-zA-Z])\1{3,}', r'\1\1', linhas[i].replace('“', '"').replace('”', '"'))
                p = re.sub(r'^[-–—]\s*', '— ', p, flags=re.MULTILINE)
                if eh_traducao or (p and p[-1] in ['.', '!', '?', ':', '"', '—']):
                    cap_atual["conteudo"].append(p + "\n\n")
                else:
                    cap_atual["conteudo"].append(p + " ")
                i += 1
                pb.update(1)
                
    if cap_atual["conteudo"]:
        cap_atual["conteudo"] = "".join(cap_atual["conteudo"]).strip()
        capitulos.append(cap_atual)

    if len(capitulos) == 1 and len(capitulos[0]["conteudo"]) > 50000:
        texto_gigante = capitulos[0]["conteudo"]
        novos_caps = []
        for idx, start in enumerate(range(0, len(texto_gigante), 40000)):
            novos_caps.append({"titulo": f"Parte {idx+1:02d}", "conteudo": texto_gigante[start:start+40000]})
        return novos_caps
        
    return capitulos

# --- CAMADA DE SÍNTESE E ESCRITA (L) ---
async def baixar_bloco_audio(idx, texto, voz, semaforo_tts, pb_cap, pb_liv):
    async with semaforo_tts:
        espera = 5
        for _ in range(6):
            buf = bytearray()
            try:
                async def fetch():
                    import edge_tts
                    comm = edge_tts.Communicate(texto, voz)
                    async for chunk in comm.stream():
                        if chunk["type"] == "audio": buf.extend(chunk["data"])
                await asyncio.wait_for(fetch(), timeout=60.0)
                if len(buf) > 100:
                    pb_cap.update(1); pb_liv.update(1)
                    return idx, buf
                raise ValueError("Vazio")
            except Exception as e:
                logging.warning(f"Falha temporária no download de áudio (bloco {idx}): {e}")
                await asyncio.sleep(espera)
                espera += 5
        logging.error(f"Falha definitiva ao baixar bloco de áudio {idx}. Áudio omitido.")
        pb_cap.update(1); pb_liv.update(1)
        return idx, bytearray()

def calcular_duracao_res(resultados):
    import io
    data_bytes = b"".join([data for _, data in resultados])
    try:
        audio_temp = MP3(io.BytesIO(data_bytes))
        return int(audio_temp.info.length * 1000)
    except: return 0

def persistir_no_disco(caminho, resultados, titulo, autor, album, faixa, modo_pasta, eh_append=False, capitulos_info=None, is_final=False):
    import io
    data_bytes = b"".join([data for _, data in resultados])
    modo = "ab" if eh_append else "wb"
    with open(caminho, modo) as f:
        f.write(data_bytes)
        
    duracao_ms = 0
    try:
        audio_temp = MP3(io.BytesIO(data_bytes))
        duracao_ms = int(audio_temp.info.length * 1000)
    except: pass

    # Se for modo pasta, ou se for a última chamada do arquivo monolítico, adiciona as tags
    if modo_pasta or is_final:
        try:
            audio = MP3(caminho, ID3=ID3)
            if audio.tags is None: audio.add_tags()
            
            titulo_tag = titulo if modo_pasta else album
            audio.tags.add(TIT2(encoding=3, text=titulo_tag))
            audio.tags.add(TALB(encoding=3, text=album))
            audio.tags.add(TRCK(encoding=3, text=str(faixa)))
            audio.tags.add(TPE1(encoding=3, text=autor))
            
            if capitulos_info and not modo_pasta:
                child_ids = []
                for cid, ctit, start_ms, end_ms in capitulos_info:
                    chap = CHAP(element_id=cid, start_time=start_ms, end_time=end_ms, 
                                sub_frames=[TIT2(encoding=3, text=ctit)])
                    audio.tags.add(chap)
                    child_ids.append(cid)
                ctoc = CTOC(element_id=u"toc", flags=3, child_element_ids=child_ids, 
                            sub_frames=[TIT2(encoding=3, text=u"Table of Contents")])
                audio.tags.add(ctoc)
                
            audio.save()
        except Exception as e:
            logging.error(f"Erro ao salvar tags ID3 no arquivo {caminho}: {e}", exc_info=True)
            
    return duracao_ms

# --- FLUXO PRINCIPAL ---
async def main():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)

    print(f"\n{Style.BRIGHT}{Fore.MAGENTA}=== 🚀 GERADOR DE AUDIOBOOK PRO ==={Style.RESET_ALL}")
    
    print(f"\n{Fore.CYAN}📂 Por favor, selecione os arquivos de livro (EPUB, PDF, etc) na janela que se abriu...{Style.RESET_ALL}")
    arquivos_selecionados = filedialog.askopenfilenames(
        title="Selecione os livros para converter",
        filetypes=[("Ebooks e Textos", "*.epub *.pdf *.mobi *.azw *.txt"), ("Todos os Arquivos", "*.*")]
    )
    if not arquivos_selecionados: 
        return print(f"{Fore.RED}❌ Nenhum arquivo selecionado. Encerrando.{Style.RESET_ALL}")

    print(f"\n{Fore.CYAN}📁 Selecione a pasta onde os audiobooks serão salvos...{Style.RESET_ALL}")
    pasta_destino = filedialog.askdirectory(title="Onde salvar os audiobooks gerados?")
    if not pasta_destino:
        return print(f"{Fore.RED}❌ Nenhuma pasta de destino selecionada. Encerrando.{Style.RESET_ALL}")
        
    root.destroy()
    
    rotas_saida = [pasta_destino]
    if os.path.exists(r"G:\Meu Drive"):
        rotas_saida.append(r"G:\Meu Drive\Audiobooks")
        status_drive = f"{Fore.GREEN}Online ☁️"
    else: status_drive = f"{Fore.RED}Offline"
    
    print(f"{Fore.WHITE}Nuvem Drive: {status_drive}")
    print("-" * 50)
    for i, f in enumerate(arquivos_selecionados, 1): 
        print(f"{Fore.CYAN}{i}.{Fore.WHITE} {os.path.basename(f)}")

    livros_para_processar = []
    print(f"\n{Fore.BLUE}🔍 Analisando idiomas e metadados...{Style.RESET_ALL}")
    for caminho_arq in arquivos_selecionados:
        arq = os.path.basename(caminho_arq)
        texto = extrair_texto(caminho_arq)
        if not texto: continue
        
        tam = len(texto)
        if tam < 1000:
            amostra_idioma = texto
        else:
            p1, p2, p3 = int(tam * 0.15), int(tam * 0.5), int(tam * 0.8)
            amostra_idioma = f"{texto[p1:p1+1500]}\n{texto[p2:p2+1500]}\n{texto[p3:p3+1500]}"
            
        try:
            idioma = detect(amostra_idioma)
        except:
            idioma = 'pt'
            
        eh_trad = False
        if idioma != 'pt':
            print(f"⚠️ {arq} detectado em {Fore.RED}{idioma.upper()}{Fore.WHITE}.")
            if input(f"Traduzir para Português? (s/n): ").lower() == 's': eh_trad = True
        
        print(f"\n{Fore.CYAN}✏️  Metadados para: {arq}{Style.RESET_ALL}")
        t = input(f"Título (Enter para original): ").strip() or os.path.splitext(arq)[0]
        a = input(f"Autor: ").strip() or "Autor Desconhecido"
        livros_para_processar.append({
            "arq": arq, "texto": texto, "trad": eh_trad,
            "titulo": t, "autor": a
        })

    precisa_traduzir = any(l["trad"] for l in livros_para_processar)
    motor_traducao = "google"
    api_key_gemini = ""

    if precisa_traduzir:
        print(f"\n{Fore.YELLOW}🧠 Configuração de Tradução:{Style.RESET_ALL}")
        print("1. Google Tradutor (Rápido, Gratuito, Tradução Simples)")
        print("2. Google Gemini IA (Tradução Profissional com Contexto - Requer API Key Gratuita)")
        esc_motor = input(f"👉 Escolha o motor de tradução (padrão 1): ") or "1"
        motor_traducao = "gemini" if esc_motor == "2" else "google"
        
        if motor_traducao == "gemini":
            try:
                with open(".env", "r") as f:
                    for linha in f:
                        if linha.startswith("GEMINI_API_KEY="): api_key_gemini = linha.split("=")[1].strip()
            except: pass
            
            if not api_key_gemini:
                print(f"\n{Fore.RED}⚠️ Chave da API do Gemini não encontrada.{Style.RESET_ALL}")
                print("Obtenha uma gratuitamente em: https://aistudio.google.com/app/apikey")
                api_key_gemini = input("👉 Cole sua GEMINI_API_KEY aqui: ").strip()
                if api_key_gemini:
                    with open(".env", "a") as f: f.write(f"\nGEMINI_API_KEY={api_key_gemini}\n")
                else:
                    print(f"{Fore.RED}Nenhuma chave fornecida. Retornando para o Google Tradutor.{Style.RESET_ALL}")
                    motor_traducao = "google"

    for l in livros_para_processar:
        l["motor"] = motor_traducao
        l["api_key"] = api_key_gemini

    print(f"\n{Fore.YELLOW}⚙️ Configurações de Áudio:{Style.RESET_ALL}")
    print("1. Arquivo Único (Áudio Monolítico)")
    print("2. Estrutura de Álbum (Pastas por Capítulo)")
    modo_pasta = (input("Opção (padrão 2): ") or "2") == "2"

    print(f"\n{Fore.YELLOW}🎤 Narradores Disponíveis:{Style.RESET_ALL}")
    for k, v in VOZES_MAP.items(): print(f"{k}. {v[1]}")
    voz_final = VOZES_MAP.get(input("Escolha (padrão 1): ") or "1", VOZES_MAP["1"])[0]

    # RELATÓRIO PREDITIVO TABELADO
    print(f"\n{Style.BRIGHT}{Fore.BLUE}📊 RELATÓRIO PREDITIVO DE PROCESSAMENTO{Style.RESET_ALL}")
    t_internet = 0; t_conv_total = 0
    for l in livros_para_processar:
        n_chars = len(l["texto"])
        seg_aud = n_chars / 15
        mb = (seg_aud / 60) * 0.35
        conv_s = (n_chars / 2500) + (n_chars / 1500 if l["trad"] else 0)
        t_internet += mb; t_conv_total += conv_s
        print(f"   • {Fore.CYAN}{l['titulo'][:25]:<25}{Fore.WHITE} | Áudio: {formatar_tempo_hms(seg_aud)} | Conv: {formatar_tempo_hms(conv_s)} | ~{mb:.1f} MB")

    if len(livros_para_processar) > 1:
        print(f"\n   {Fore.MAGENTA}Total Lote: ~{t_internet:.1f} MB | Espera Total: ~{formatar_tempo_hms(t_conv_total)}")
    
    input(f"\n{Fore.GREEN}Pressione [ENTER] para iniciar a produção...{Style.RESET_ALL}")
    print(f"\n{Style.BRIGHT}{Fore.MAGENTA}🔥 INICIANDO PIPELINE EM LOTE...{Style.RESET_ALL}\n")

    # GESTÃO DE PARALELISMO
    exibir_lote_g = len(livros_para_processar) > 1
    off_g = 1 if exibir_lote_g else 0
    l_por_livro = 2 if modo_pasta else 1
    semaforo_g = asyncio.Semaphore(4)
    slots_ui = asyncio.Queue()
    for i in range(2): slots_ui.put_nowait(i)

    async def task_livro(livro, pb_global):
        slot = await slots_ui.get()
        pos_base = off_g + (slot * l_por_livro)
        p_cap, p_liv = (pos_base, pos_base + 1) if modo_pasta else (0, pos_base)

        try:
            txt_l = livro["texto"]
            if livro["trad"]:
                txt_l = await traduzir_texto(txt_l, livro["arq"], p_liv, motor=livro["motor"], api_key=livro["api_key"])
            
            caps = await asyncio.to_thread(estruturar_obra_nlp, txt_l, livro["trad"], p_liv)

            nome_dir = re.sub(r'[\\/*?:\'\"<>|]', '', livro["titulo"])
            dests = []
            for r in rotas_saida:
                p = os.path.join(r, f"{nome_dir} (Audiobook)") if modo_pasta else r
                os.makedirs(p, exist_ok=True); dests.append(p)
                
            logging.info(f"Livro '{livro['titulo']}' estruturado. Inciando síntese de áudio.")
            if not modo_pasta:
                for d in dests:
                    arq_f = os.path.join(d, f"{nome_dir}.mp3")
                    if os.path.exists(arq_f): os.remove(arq_f)

            jobs = []; total_p = 0
            for c in caps:
                narr = f"{c['titulo']}. . . {c['conteudo']}"
                narr = otimizar_texto_ptbr(narr)
                partes = [narr[i:i+3000] for i in range(0, len(narr), 3000)]
                jobs.append((c['titulo'], partes)); total_p += len(partes)

            capitulos_info = []
            current_time_ms = 0

            with tqdm(total=total_p, desc=f"{Fore.BLUE}📖 {livro['titulo'][:12]}", position=p_liv, leave=False, colour="blue", bar_format=F_LIVRO) as pb_l:
                for idx_f, (tit_c, pedacos) in enumerate(jobs, start=1):
                    with tqdm(total=len(pedacos), desc=f"{Fore.GREEN}🎧 {tit_c[:12]}", position=p_cap, leave=False, colour="green", bar_format=F_CAP, disable=not modo_pasta) as pb_c:
                        tasks = [baixar_bloco_audio(idx, p, voz_final, semaforo_g, pb_c, pb_l) for idx, p in enumerate(pedacos)]
                        res = await asyncio.gather(*tasks)
                    res.sort(key=lambda x: x[0])
                    
                    duracao = await asyncio.to_thread(calcular_duracao_res, res)
                    end_time_ms = current_time_ms + duracao
                    
                    if not modo_pasta:
                        capitulos_info.append((f"ch{idx_f}", tit_c, current_time_ms, end_time_ms))
                    
                    for d in dests:
                        arq_f = f"{tit_c}.mp3" if modo_pasta else f"{nome_dir}.mp3"
                        caminho_arq = os.path.join(d, arq_f)
                        is_final_cap = (idx_f == len(jobs))
                        
                        await asyncio.to_thread(
                            persistir_no_disco, caminho_arq, res, tit_c, livro["autor"], 
                            livro["titulo"], idx_f, modo_pasta, eh_append=(not modo_pasta),
                            capitulos_info=capitulos_info, is_final=(not modo_pasta and is_final_cap)
                        )
                        
                    if not modo_pasta:
                        current_time_ms = end_time_ms
            logging.info(f"Livro '{livro['titulo']}' processado com sucesso.")
        except Exception as e:
            logging.error(f"Erro crítico no processamento do livro {livro.get('arq')}: {e}", exc_info=True)
            print(f"\n{Fore.RED}❌ Erro durante o processamento do livro {livro.get('arq')}. Verifique o log.{Style.RESET_ALL}")
        finally:
            if not pb_global.disable: pb_global.update(1)
            slots_ui.put_nowait(slot); del livro["texto"]; gc.collect()

    with tqdm(total=len(livros_para_processar), desc=f"{Fore.MAGENTA}📚 Lote Global", position=0, colour="magenta", bar_format=F_LOTE, disable=not exibir_lote_g) as pb_g:
        await asyncio.gather(*[task_livro(l, pb_g) for l in livros_para_processar])

    print(f"\n\n\n{Style.BRIGHT}{Fore.GREEN}✅ SUCESSO ABSOLUTO! Seus audiobooks estão prontos em '{pasta_destino}'.{Style.RESET_ALL} 🎉")

if __name__ == "__main__":
    try: 
        logging.info("--- INÍCIO DA EXECUÇÃO DO APLICATIVO ---")
        asyncio.run(main())
        logging.info("--- FIM DA EXECUÇÃO DO APLICATIVO ---")
    except KeyboardInterrupt: 
        logging.warning("Processo interrompido pelo usuário.")
        print(f"\n{Fore.RED}🛑 Interrompido pelo usuário.")
    except Exception as e:
        logging.error(f"Erro fatal não tratado na execução principal: {e}", exc_info=True)
        print(f"\n{Fore.RED}🛑 Erro fatal. Verifique o arquivo audiobook_generator.log{Style.RESET_ALL}")