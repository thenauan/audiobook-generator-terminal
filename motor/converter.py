"""
Conversor de e-books em audiobooks.

Fluxo de cada livro:
  1. Capítulos: os enviados pelo app (detectados ao carregar e revisados pelo usuário) ou os do
     book_structure (sumário do livro, títulos do HTML ou heurística).
  2. Tradução (opcional), capítulo por capítulo, para o idioma da voz.
  3. Texto para o narrador: dicionário de pronúncia, abreviações e pontuação (book_structure).
  4. Síntese: blocos de até 2.500 caracteres, em paralelo entre todos os capítulos e livros (limite
     global de conexões), cada bloco salvo em disco assim que chega (retomada sem perder nada).
  5. Cada capítulo é normalizado (EBU R128) e guardado sem perda; o arquivo final é codificado uma vez.
  6. Texto sincronizado (opcional) com o texto exato que foi narrado.
"""
import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import subprocess

# O conversor roda fora do console do app (para o Ctrl+C não derrubá-lo). Sem console, cada programa que
# ele abre (FFmpeg etc.) criaria uma janela própria na tela: no Windows, todos abrem sem janela.
_CLASSE_PRIORIDADE = 0x00004000  # BELOW_NORMAL; IDLE (0x40) no modo leve, NORMAL (0x20) no turbo
if os.name == "nt":
    _popen_init = subprocess.Popen.__init__

    def _popen_sem_janela(self, *args, **kwargs):
        # CREATE_NO_WINDOW + prioridade da conversão (abaixo do normal; mínima no modo leve)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000 | _CLASSE_PRIORIDADE
        _popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_sem_janela

import urllib.error
import urllib.parse
import urllib.request

try:
    from deep_translator import GoogleTranslator
except Exception:  # biblioteca opcional: o caminho principal não depende dela
    GoogleTranslator = None
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TALB, TRCK, TPE1, TPE2, TCON, APIC, CHAP, CTOC

from audio_output import (aplicar_ano, FORMATOS, EXTENSOES_AUDIO, formato, qualidade, normalizar_capitulo, duracao_ms,
                          gerar_arquivo_unico, codificar_capitulo, obter_communicate)
from book_structure import estruturar_livro, preparar_para_narracao, titulo_falado, limpar_texto, texto_completo, LivroIlegivel

# Conexões simultâneas com o serviço de voz (todas as conversões somadas) e conversões do FFmpeg ao mesmo tempo
TTS_CONEXOES = 10  # medido: 10 conexões dobram a velocidade; acima disso o serviço limita
FFMPEG_PARALELO = max(1, min(4, (os.cpu_count() or 2) // 2))
TAMANHO_BLOCO = 2500
PAUSA_INICIAL_S = 1.5  # pausa no começo de cada capítulo (normalizar_capitulo)
_sem_tts = None
_sem_ffmpeg = None
_sem_traducao = None


# Última etapa de cada livro (vai nos detalhes dos erros)
_ETAPA = {}
_NOMES_ETAPA = {"translating": "tradução", "editing": "preparação do texto", "processing": "narração",
                "syncing": "montagem do áudio e texto sincronizado"}
# Códigos estáveis das etapas e mensagens: além do texto em português, cada evento leva "code" (e "args"
# quando o texto tem valores; "unitCode" para partsUnit), para outras interfaces traduzirem sem depender do texto
_CODIGOS_ETAPA = {"translating": "stage_translation", "editing": "stage_preparation", "processing": "stage_narration",
                  "syncing": "stage_sync"}
_ETAPA_COD = {}


def emit(event):
    # Eventos para o servidor (repassados ao app por Server-Sent Events)
    fid = event.get("fileId")
    if fid and event.get("status") in _NOMES_ETAPA:
        _ETAPA[fid] = _NOMES_ETAPA[event["status"]]
        _ETAPA_COD[fid] = _CODIGOS_ETAPA[event["status"]]
    if event.get("type") == "error" and fid and "details" not in event:
        event = {**event, "details": f"Etapa: {_ETAPA.get(fid, 'início')}"}
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _detalhes_erro(file_id, e, rastreio=None):
    linhas = [f"Etapa: {_ETAPA.get(file_id, 'início')}", f"Erro: {type(e).__name__}: {e}"]
    if rastreio:
        linhas += ["", "Rastreio:", rastreio.strip()[-3000:]]
    return "\n".join(linhas)


def get_unique_path(base_path):
    if not os.path.exists(base_path):
        return base_path
    name, ext = os.path.splitext(base_path)
    counter = 1
    while os.path.exists(f"{name} ({counter}){ext}"):
        counter += 1
    return f"{name} ({counter}){ext}"


def extrair_texto(caminho):
    """Texto inteiro do livro, já limpo (compatibilidade com módulos antigos)."""
    try:
        return texto_completo(caminho)
    except Exception as e:
        emit({"type": "log", "message": f"Erro ao extrair texto: {e}"})
        return None


def idioma_da_voz(voz: str) -> str:
    return (voz or "pt-BR").split("-")[0].lower()


def dividir_em_blocos_seguros(texto: str, max_chars: int = TAMANHO_BLOCO) -> list:
    """Blocos de até max_chars respeitando parágrafos e frases (nunca corta palavras)."""
    if not texto:
        return []
    texto = texto.strip()
    if len(texto) <= max_chars:
        return [texto]
    blocos, atual, tam = [], [], 0

    def fecha():
        nonlocal atual, tam
        if atual:
            blocos.append("\n\n".join(atual))
        atual, tam = [], 0

    for p in (p.strip() for p in re.split(r"\n\s*\n", texto)):
        if not p:
            continue
        if tam + len(p) + 2 <= max_chars:
            atual.append(p)
            tam += len(p) + 2
            continue
        fecha()
        if len(p) <= max_chars:
            atual, tam = [p], len(p)
            continue
        # Parágrafo enorme: divide por frases e, em último caso, por palavras
        frase_atual = ""
        for f in re.split(r"(?<=[.!?…;:])\s+|(?<=[。！？；])", p):
            if len(f) > max_chars:
                for w in f.split(" "):
                    # Escritas sem espaço entre palavras: corta no limite
                    while len(w) > max_chars:
                        if frase_atual.strip():
                            blocos.append(frase_atual.strip())
                            frase_atual = ""
                        blocos.append(w[:max_chars])
                        w = w[max_chars:]
                    if len(frase_atual) + len(w) + 1 > max_chars:
                        blocos.append(frase_atual.strip())
                        frase_atual = ""
                    frase_atual += " " + w
                continue
            if len(frase_atual) + len(f) + 1 > max_chars:
                blocos.append(frase_atual.strip())
                frase_atual = ""
            frase_atual += " " + f
        if frase_atual.strip():
            atual, tam = [frase_atual.strip()], len(frase_atual)
    fecha()
    return [b for b in blocos if b.strip()]


def formatar_nome_capitulo(tit_c: str, idx_f: int, total_caps: int) -> str:
    """'01 - Título' com zeros à esquerda, para a ordem certa em qualquer player."""
    pad = max(2, len(str(total_caps)))
    nome = f"{idx_f:0{pad}d} - {tit_c.strip()}"
    return re.sub(r'[\\/*?:\'"<>|]', "", nome).strip()[:150]


def _hash(*partes) -> str:
    h = hashlib.sha1()
    for p in partes:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:20]


# ---------------------------------------------------------------------------------------------
# Tradução
# ---------------------------------------------------------------------------------------------
class FalhaTraducao(RuntimeError):
    pass


# O tradutor gratuito aceita ~5 pedidos por segundo: no máximo 3 ao mesmo tempo e um intervalo entre eles
_INTERVALO_TRADUCAO_S = 0.4
_ultimo_pedido = {"t": 0.0}
_CACHE_TRADUCAO = os.path.join(tempfile.gettempdir(), "audiobook_generator_traducoes")


def _traduzir_google(texto: str, alvo: str) -> str:
    """Tradução pelo endpoint público do Google Tradutor (o mesmo do widget do navegador). Se ele
    recusar, tenta pelo deep_translator, que usa outra página do Google."""
    erro = None
    try:
        corpo = urllib.parse.urlencode({"q": texto}).encode("utf-8")
        url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
            {"client": "gtx", "sl": "auto", "tl": alvo, "dt": "t", "ie": "UTF-8", "oe": "UTF-8"})
        req = urllib.request.Request(url, data=corpo, headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        })
        with urllib.request.urlopen(req, timeout=40) as r:
            dados = json.loads(r.read().decode("utf-8"))
        res = "".join(seg[0] for seg in (dados[0] or []) if seg and seg[0])
        if res.strip():
            return res
        erro = ValueError("tradução vazia")
    except urllib.error.HTTPError as e:
        erro = RuntimeError("TooManyRequests" if e.code == 429 else f"HTTP {e.code}")
    except Exception as e:
        erro = e
    if GoogleTranslator is not None:
        return GoogleTranslator(source="auto", target=alvo).translate(texto)
    raise erro


async def _traduzir_bloco(bloco, alvo, ao_concluir):
    """Traduz um trecho. Usa o cache (uma nova tentativa não traduz de novo o que já foi traduzido) e
    espera mais a cada recusa do serviço. Se não conseguir, levanta FalhaTraducao: narrar o texto
    original com a voz de outro idioma estragaria o audiobook."""
    if not re.search(r"\w", bloco):
        ao_concluir()
        return bloco
    cache = os.path.join(_CACHE_TRADUCAO, f"{alvo}_{_hash(alvo, bloco)}.txt")
    try:
        with open(cache, "r", encoding="utf-8") as f:
            ao_concluir()
            return f.read()
    except OSError:
        pass
    async with _sem_traducao:
        espera = 5
        ultimo = None
        for tentativa in range(5):
            atraso = _ultimo_pedido["t"] + _INTERVALO_TRADUCAO_S - asyncio.get_running_loop().time()
            if atraso > 0:
                await asyncio.sleep(atraso)
            _ultimo_pedido["t"] = asyncio.get_running_loop().time()
            try:
                res = await asyncio.wait_for(asyncio.to_thread(_traduzir_google, bloco, alvo), timeout=90.0)
                if res and res.strip():
                    try:
                        os.makedirs(_CACHE_TRADUCAO, exist_ok=True)
                        with open(cache, "w", encoding="utf-8") as f:
                            f.write(res)
                    except OSError:
                        pass
                    ao_concluir()
                    return res
                raise ValueError("tradução vazia")
            except Exception as e:
                ultimo = e
                emit({"type": "log", "message": f"Tradução: nova tentativa em {espera}s ({tentativa + 1}/5) - {type(e).__name__}"})
                await asyncio.sleep(espera)
                espera = min(espera * 2, 60)
    limite = "TooManyRequests" in type(ultimo).__name__ or "too many" in str(ultimo).lower()
    falha = FalhaTraducao(
        "O serviço de tradução do Google recusou os pedidos por excesso de uso. Espere alguns minutos e converta de novo "
        "(o que já foi traduzido fica guardado), ou converta sem traduzir."
        if limite else
        f"A tradução falhou ({type(ultimo).__name__}). Verifique a internet e converta de novo, ou converta sem traduzir.")
    falha.codigo, falha.args_codigo = ("err_translation_limit", []) if limite else ("err_translation", [type(ultimo).__name__])
    raise falha


def _blocos_traducao(texto: str, limite: int = 3000):
    blocos, atual, tam = [], [], 0
    for p in texto.split("\n\n"):
        if tam + len(p) > limite and atual:
            blocos.append("\n\n".join(atual))
            atual, tam = [], 0
        atual.append(p)
        tam += len(p) + 2
    if atual:
        blocos.append("\n\n".join(atual))
    return blocos


async def traduzir_capitulos(caps, file_id, alvo):
    """Traduz títulos e conteúdos de todos os capítulos, com um progresso único."""
    blocos_cap = [_blocos_traducao(c["conteudo"]) for c in caps]
    total = max(1, sum(len(b) for b in blocos_cap) + 1)
    feitos = {"n": 0}

    def ao_concluir():
        feitos["n"] += 1
        emit({"type": "progress", "fileId": file_id, "status": "translating", "progress": round(3 + min(1, feitos["n"] / total) * 12, 1),
              "message": "Traduzindo o texto", "code": "engine_translating", "partsDone": min(feitos["n"], total), "partsTotal": total,
              "partsUnit": "trechos traduzidos", "unitCode": "unit_translated"})

    # Títulos: um pedido só (uma linha por título); se o tradutor juntar ou separar linhas, traduz um por um
    titulos = [c["titulo"].replace("\n", " ").strip() for c in caps]
    traduzidos = (await _traduzir_bloco("\n".join(titulos), alvo, ao_concluir)).split("\n") if titulos else []
    if len(traduzidos) != len(titulos):
        traduzidos = [await _traduzir_bloco(t, alvo, lambda: None) for t in titulos]

    async def conteudo(blocos):
        return "\n\n".join(await asyncio.gather(*[_traduzir_bloco(b, alvo, ao_concluir) for b in blocos]))

    textos = await asyncio.gather(*[conteudo(b) for b in blocos_cap])
    for c, tit, txt in zip(caps, traduzidos, textos):
        c["titulo"] = tit.strip() or c["titulo"]
        c["conteudo"] = limpar_texto(txt)
    return caps


# ---------------------------------------------------------------------------------------------
# Síntese de voz
# ---------------------------------------------------------------------------------------------
class FalhaSintese(RuntimeError):
    pass


def _ler_json(caminho, padrao):
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return padrao


def _salvar_json(caminho, dados):
    try:
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False)
    except Exception:
        pass


def _duracao_mp3_s(caminho):
    try:
        return float(MP3(caminho).info.length)
    except Exception:
        return duracao_ms(caminho) / 1000.0


_FIM_FRASE = re.compile(r"[.!?…。！？]")


def _frases_de_palavras(texto, palavras):
    """Monta as frases a partir dos tempos das palavras: cada palavra é localizada no texto do bloco e a
    frase termina na pontuação final (ou na quebra de parágrafo) que vem depois dela. Assim uma única
    narração dá os tempos das palavras e das frases. Formato de saída: [início, duração, texto]."""
    frases, cursor, ini_txt, t0, t_fim = [], 0, None, 0.0, 0.0
    for off, dur, palavra in palavras:
        pos = texto.find(palavra, cursor) if palavra else -1
        if pos < 0 or pos - cursor > 200:  # palavra normalizada pelo serviço (número, sigla): segue em frente
            pos = cursor
            fim = cursor
        else:
            fim = pos + len(palavra)
        if ini_txt is None:
            ini_txt, t0 = pos, off
        t_fim = off + dur
        cursor = fim
        seguinte = re.match(r"[^\w]*", texto[cursor:]).group(0)
        depois = texto[cursor + len(seguinte):cursor + len(seguinte) + 1]
        # Reticências seguidas de minúscula continuam a mesma frase ("e então... ela saiu")
        so_reticencias = re.fullmatch(r"\s*(?:\.\.\.|…)\s*", seguinte) and depois.islower()
        if (_FIM_FRASE.search(seguinte) and not so_reticencias) or "\n" in seguinte:
            corte = cursor + len(seguinte.rstrip())
            txt = re.sub(r"\s+", " ", texto[ini_txt:corte]).strip()
            if txt:
                frases.append([round(t0, 3), round(t_fim - t0, 3), txt])
            ini_txt = None
    if ini_txt is not None:
        txt = re.sub(r"\s+", " ", texto[ini_txt:]).strip()
        if txt:
            frases.append([round(t0, 3), round(t_fim - t0, 3), txt])
    return frases


# Conexões com o serviço de voz divididas por igual entre os livros sendo narrados (antes o primeiro livro
# pegava todas e os outros ficavam parados em 0 partes). Quando um termina, os outros ganham a parte dele.
_NARRANDO = set()
_EM_USO = {}
_vagas = None
# Bytes de áudio recebidos por livro, para a velocidade real mostrada no card
_BYTES = {}


_TRAVAS = []


def _processo_vivo(pid):
    """Se o processo existe (sem encerrá-lo: no Windows, os.kill(pid, 0) o finalizaria)."""
    try:
        pid = int(pid)
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            codigo = ctypes.c_ulong()
            k.GetExitCodeProcess(h, ctypes.byref(codigo))
            k.CloseHandle(h)
            return codigo.value == 259  # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _limite_por_livro():
    return max(1, -(-TTS_CONEXOES // max(1, len(_NARRANDO))))


async def _pegar_vaga(fid):
    async with _vagas:
        await _vagas.wait_for(lambda: _EM_USO.get(fid, 0) < _limite_por_livro())
        _EM_USO[fid] = _EM_USO.get(fid, 0) + 1


async def _soltar_vaga(fid):
    async with _vagas:
        _EM_USO[fid] = max(0, _EM_USO.get(fid, 0) - 1)
        _vagas.notify_all()


async def _mudar_narrando(fid, entra):
    async with _vagas:
        (_NARRANDO.add if entra else _NARRANDO.discard)(fid)
        _vagas.notify_all()


async def sintetizar_bloco(texto, voz, destino, ao_concluir, com_palavras=False, fid=None):
    """Gera o áudio de um bloco direto em disco, com o tempo de cada frase (do próprio serviço de voz) e,
    se pedido, de cada palavra. Usa o cache se já existir; tenta de novo se falhar."""
    if os.path.exists(destino) and os.path.getsize(destino) > 150:
        ao_concluir()
        return destino
    if fid is not None:
        await _pegar_vaga(fid)
    try:
        return await _sintetizar_com_conexao(texto, voz, destino, ao_concluir, com_palavras, fid)
    finally:
        if fid is not None:
            await _soltar_vaga(fid)


async def _sintetizar_com_conexao(texto, voz, destino, ao_concluir, com_palavras, fid):
    async with _sem_tts:
        espera = 3
        ultimo_erro = None
        for tentativa in range(8):
            parcial = destino + ".part"
            try:
                marcas = []

                async def baixa():
                    classe = obter_communicate()
                    try:
                        comm = classe(texto, voz, boundary="WordBoundary" if com_palavras else "SentenceBoundary")
                    except TypeError:  # edge-tts antigo, sem a opção: só frases
                        comm = classe(texto, voz)
                    with open(parcial, "wb") as f:
                        async for chunk in comm.stream():
                            if chunk["type"] == "audio":
                                f.write(chunk["data"])
                                if fid is not None:
                                    _BYTES[fid] = _BYTES.get(fid, 0) + len(chunk["data"])
                            elif chunk["type"] in ("SentenceBoundary", "WordBoundary"):
                                # offset e duração em unidades de 100 ns
                                marcas.append([chunk["offset"] / 10_000_000, chunk["duration"] / 10_000_000,
                                               html.unescape(chunk.get("text", "")), chunk["type"]])
                await asyncio.wait_for(baixa(), timeout=120.0)
                if os.path.getsize(parcial) > 150:
                    palavras = [m[:3] for m in marcas if m[3] == "WordBoundary"]
                    frases = [m[:3] for m in marcas if m[3] == "SentenceBoundary"]
                    if palavras and not frases:
                        frases = _frases_de_palavras(texto, palavras)
                    _salvar_json(destino + ".json", {"frases": frases, "palavras": palavras})
                    os.replace(parcial, destino)
                    ao_concluir()
                    return destino
                raise ValueError("o serviço de voz não devolveu áudio")
            except Exception as e:
                ultimo_erro = e
                emit({"type": "log", "message": f"Voz: nova tentativa {tentativa + 1}/8 em {espera}s - {type(e).__name__}: {str(e)[:120]}"})
                await asyncio.sleep(espera)
                espera = min(espera * 2, 40)
            finally:
                if os.path.exists(parcial):
                    try:
                        os.remove(parcial)
                    except OSError:
                        pass
    falha = FalhaSintese(f"O serviço de voz não respondeu depois de várias tentativas ({type(ultimo_erro).__name__}). "
                         "Verifique a internet e converta de novo: o que já foi narrado fica salvo e a conversão continua de onde parou.")
    falha.codigo, falha.args_codigo = "err_tts", [type(ultimo_erro).__name__]
    raise falha


def _marcas_do_bloco(dados):
    """Tempos salvos de um bloco: {"frases", "palavras"} (atual) ou a lista de frases (versões antigas)."""
    if isinstance(dados, dict):
        return dados.get("frases") or [], dados.get("palavras") or []
    return dados or [], []


async def montar_capitulo(idx_f, titulo_fala, texto, voz, cache_dir, progresso, com_palavras=False, fid=None, pronto=None):
    """Sintetiza os blocos de um capítulo (em paralelo com os outros).
    Devolve (arquivo do capítulo, frases e palavras com tempo em segundos a partir do início do capítulo)."""
    partes = dividir_em_blocos_seguros(f"{titulo_fala}.\n\n{texto}" if texto else f"{titulo_fala}.")
    partes = [p for p in partes if re.search(r"\w", p)]  # blocos só com pontuação não geram áudio
    # O tempo das palavras exige outra narração (cache próprio); só frases mantém o cache de antes
    extra = ("palavras",) if com_palavras else ()
    chave = _hash(voz, *extra, *partes)
    final = os.path.join(cache_dir, f"cap_{idx_f:03d}_{chave}.flac")
    linha_tempo = final + ".json"
    if os.path.exists(final) and os.path.getsize(final) > 1024:
        progresso(len(partes))
        if pronto:
            pronto()
        frases, palavras = _marcas_do_bloco(_ler_json(linha_tempo, []))
        return final, frases, palavras

    caminhos = await asyncio.gather(*[
        sintetizar_bloco(p, voz, os.path.join(cache_dir, f"bloco_{_hash(voz, *extra, p)}.mp3"), lambda: progresso(1), com_palavras, fid)
        for p in partes])

    # Linha do tempo das frases e palavras: cada bloco começa onde o anterior terminou
    frases, palavras, inicio_bloco = [], [], 0.0
    for c, parte in zip(caminhos, partes):
        marcas, marcas_palavras = _marcas_do_bloco(_ler_json(c + ".json", []))
        dur = _duracao_mp3_s(c)
        if not marcas:  # bloco sem tempos: uma frase só para o bloco inteiro
            marcas = [[0.0, dur, re.sub(r"\s+", " ", parte).strip()]]
        for off, d, txt in marcas:
            if str(txt).strip():
                frases.append([round(inicio_bloco + off, 3), round(inicio_bloco + off + d, 3), str(txt).strip()])
        for off, d, txt in marcas_palavras:
            if str(txt).strip():
                palavras.append([round(inicio_bloco + off, 3), round(inicio_bloco + off + d, 3), str(txt).strip()])
        inicio_bloco += dur

    bruto = os.path.join(cache_dir, f"cap_{idx_f:03d}_{chave}.mp3")
    with open(bruto, "wb") as saida:
        for c in caminhos:
            with open(c, "rb") as f:
                shutil.copyfileobj(f, saida)
    async with _sem_ffmpeg:
        ok = await asyncio.to_thread(normalizar_capitulo, bruto, final)
    if pronto:
        pronto()
    if not ok:
        return bruto, frases, palavras  # sem FFmpeg funcional: usa a voz como veio (sem a pausa inicial)
    frases = [[a + PAUSA_INICIAL_S, b + PAUSA_INICIAL_S, t] for a, b, t in frases]
    palavras = [[a + PAUSA_INICIAL_S, b + PAUSA_INICIAL_S, t] for a, b, t in palavras]
    _salvar_json(linha_tempo, {"frases": frases, "palavras": palavras})
    for c in [bruto, *caminhos]:
        for arq in (c, c + ".json"):
            try:
                os.remove(arq)
            except OSError:
                pass
    return final, frases, palavras


# ---------------------------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------------------------
def embutir_capa_id3(audio, cover_base64):
    if not cover_base64:
        return
    try:
        import base64
        clean_b64 = str(cover_base64).strip()
        mime = 'image/jpeg'
        if clean_b64.startswith('data:'):
            header, clean_b64 = clean_b64.split(',', 1)
            if 'image/png' in header:
                mime = 'image/png'
            elif 'image/webp' in header:
                mime = 'image/webp'
            elif 'image/svg+xml' in header:
                return
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc=u'Cover', data=base64.b64decode(clean_b64)))
    except Exception as e:
        emit({"type": "log", "message": f"Aviso ao embutir capa ID3: {e}"})


def aplicar_tags_capitulo_mp3(caminho, titulo, autor, album, faixa, cover_base64=None, genero=None, narrador=None):
    try:
        audio = MP3(caminho, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=titulo))
        audio.tags.add(TALB(encoding=3, text=album))
        audio.tags.add(TRCK(encoding=3, text=str(faixa)))
        audio.tags.add(TPE1(encoding=3, text=autor))
        if genero:
            audio.tags.add(TCON(encoding=3, text=str(genero)))
        if narrador:
            audio.tags.add(TPE2(encoding=3, text=str(narrador)))
        embutir_capa_id3(audio, cover_base64)
        audio.save(v2_version=3)
    except Exception as e:
        emit({"type": "log", "message": f"Erro tags ID3: {e}"})


def aplicar_tags_arquivo_unico(caminho, titulo, autor, cover_base64, chapters_info, genero=None, narrador=None):
    """Tags, capa e capítulos (CHAP/CTOC) num MP3 único."""
    try:
        audio = MP3(caminho, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=titulo))
        audio.tags.add(TALB(encoding=3, text=titulo))
        audio.tags.add(TRCK(encoding=3, text="1"))
        audio.tags.add(TPE1(encoding=3, text=autor))
        if genero:
            audio.tags.add(TCON(encoding=3, text=str(genero)))
        if narrador:
            audio.tags.add(TPE2(encoding=3, text=str(narrador)))
        embutir_capa_id3(audio, cover_base64)
        if chapters_info:
            ids = []
            for ch in chapters_info:
                cid = ch["id"].encode("utf-8")
                ids.append(cid)
                audio.tags.add(CHAP(element_id=cid, start_time=ch["start_time"], end_time=ch["end_time"],
                                    sub_frames=[TIT2(encoding=3, text=ch["title"])]))
            audio.tags.add(CTOC(element_id=b"toc", flags=3, child_element_ids=ids, sub_frames=[TIT2(encoding=3, text="Capítulos")]))
        audio.save(v2_version=3)
    except Exception as e:
        emit({"type": "log", "message": f"Erro tags ID3 arquivo unico: {e}"})


async def upload_to_drive_async(file_path, folder_id, file_id, emit):
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2.credentials import Credentials
        if not os.path.exists('token.json'):
            return
        creds = Credentials.from_authorized_user_file('token.json', ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive.metadata.readonly'])
        service = build('drive', 'v3', credentials=creds)
        file_metadata = {'name': os.path.basename(file_path), 'parents': [folder_id]} if folder_id and folder_id != 'root' else {'name': os.path.basename(file_path)}
        media = MediaFileUpload(file_path, mimetype='audio/mpeg', resumable=True)

        def upload():
            request = service.files().create(body=file_metadata, media_body=media, fields='id')
            response = None
            while response is None:
                _, response = request.next_chunk()
        await asyncio.to_thread(upload)
    except Exception as e:
        emit({"type": "log", "message": f"Erro no upload para o Drive: {e}"})


# ---------------------------------------------------------------------------------------------
# Livro
# ---------------------------------------------------------------------------------------------
def _config_do_livro(file_info, config):
    cfg = file_info.get("config") or config.get("config") or config
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _escolher(file_info, cfg, *chaves, padrao=None):
    for fonte in (file_info, file_info.get("config") or {}, cfg):
        if not isinstance(fonte, dict):
            continue
        for c in chaves:
            v = fonte.get(c)
            if v not in (None, ""):
                return v
    return padrao


async def process_file(file_info, config):
    file_id = file_info.get("id")
    try:
        caminho = file_info["path"]
        titulo = (file_info.get("title") or "").strip() or "Audiobook"
        autor = (file_info.get("author") or "").strip() or "Desconhecido"
        genero = file_info.get("genre", "")
        narrador = file_info.get("narrator", "")
        cfg = _config_do_livro(file_info, config)

        # Capa: a do e-book. Sem capa (ou só uma imagem SVG, que não vai dentro do áudio), a capa genérica do app,
        # quando pedida (a versão de terminal pede; o app gráfico mantém o comportamento de sempre)
        capa = str(file_info.get("cover") or "")
        if file_info.get("generic_cover") and (not capa or capa.startswith("data:image/svg")):
            try:
                from capa_generica import gerar_capa_generica
                gerada = gerar_capa_generica(titulo, autor, file_info.get("cover_color") or file_info.get("coverColor"))
                if gerada:
                    file_info["cover"] = gerada
            except Exception as e:
                emit({"type": "log", "message": f"Capa genérica não gerada: {e}"})

        pasta_destino = _escolher(file_info, cfg, "outputFolder", "output_dir")
        if not pasta_destino:
            emit({"type": "error", "fileId": file_id, "error": "Pasta de destino não definida. Escolha uma em Ajustes › Armazenamento e pastas.",
                  "code": "err_no_folder"})
            return
        voz = _escolher(file_info, cfg, "voice", padrao="pt-BR-AntonioNeural")
        idioma = idioma_da_voz(voz)
        fmt_saida = formato(_escolher(file_info, cfg, "fileType", "file_format", padrao="m4b"))
        qualidade_saida = qualidade(_escolher(file_info, cfg, "audio_quality"))
        gerar_sync = _escolher(file_info, cfg, "generate_sync_text", padrao=True)
        gerar_sync = gerar_sync not in (False, "false", 0, "0")
        # Separação do texto sincronizado: frases, palavras ou as duas
        separacao = _escolher(file_info, cfg, "sync_text_granularity", padrao="sentences")
        if separacao not in ("sentences", "words", "both"):
            separacao = "sentences"
        com_palavras = gerar_sync and separacao in ("words", "both")
        is_multiple = fmt_saida == "multiple"
        ext_saida = FORMATOS[fmt_saida]["ext"]

        emit({"type": "progress", "fileId": file_id, "status": "processing", "progress": 1, "message": "Lendo o livro", "code": "engine_reading"})

        # 1) Capítulos
        caps = []
        for uc in file_info.get("chapters") or []:
            conteudo = limpar_texto((uc.get("content") or uc.get("conteudo") or "").strip())
            tit = (uc.get("title") or uc.get("titulo") or "").strip()
            if conteudo:
                caps.append({"titulo": tit or f"Capítulo {len(caps) + 1}", "conteudo": conteudo})
        if not caps:
            try:
                caps = await asyncio.to_thread(estruturar_livro, caminho, idioma)
            except LivroIlegivel as e:
                emit({"type": "error", "fileId": file_id, "error": str(e), "code": e.codigo, "args": e.args_codigo})
                return
        if not caps:
            emit({"type": "error", "fileId": file_id, "error": "Não foi possível ler o texto deste arquivo (ele pode estar protegido ou ser só imagens).",
                  "code": "err_unreadable"})
            return

        # 2) Tradução para o idioma da voz
        if file_info.get("translate"):
            emit({"type": "progress", "fileId": file_id, "status": "translating", "progress": 3, "message": "Traduzindo o texto",
                  "code": "engine_translating"})
            caps = await traduzir_capitulos(caps, file_id, {"zh": "zh-CN"}.get(idioma, idioma))

        # 3) Texto do narrador (o texto sincronizado usa o texto sem esses ajustes)
        inicio_narracao = 15 if file_info.get("translate") else 3
        emit({"type": "progress", "fileId": file_id, "status": "editing", "progress": inicio_narracao, "message": "Preparando o texto",
              "code": "engine_preparing"})
        mapa = file_info.get("pronunciationMap") or file_info.get("pronunciation_map") or cfg.get("pronunciation_map") or []
        narracao = [(titulo_falado(c["titulo"]), preparar_para_narracao(c["conteudo"], voz, mapa)) for c in caps]
        emit({"type": "progress", "fileId": file_id, "status": "editing", "progress": inicio_narracao, "message": "Preparando o texto",
              "code": "engine_preparing"})

        # 4) Síntese
        pasta_destino = os.path.abspath(pasta_destino)
        # A pasta precisa existir (ou poder ser criada) e aceitar gravação
        try:
            os.makedirs(pasta_destino, exist_ok=True)
            teste = os.path.join(pasta_destino, f".teste_gravacao_{os.getpid()}")
            with open(teste, "w") as f:
                f.write("ok")
            os.remove(teste)
        except OSError:
            emit({"type": "error", "fileId": file_id, "error": f"Não foi possível gravar na pasta de destino ({pasta_destino}). "
                                                                "Verifique se ela existe (um HD externo pode ter sido desconectado) ou escolha outra em Ajustes › Armazenamento e pastas.",
                  "code": "err_write", "args": [pasta_destino]})
            return
        # Espaço em disco: áudio final + cache sem perda durante a conversão
        minutos = sum(len(x) for _, x in narracao) / 900
        kbps = {"m4b": 256, "opus": 128, "flac": 210, "wav": 384}.get(fmt_saida, 320)
        precisa_mb = minutos * 60 * (kbps + 250) / 8 / 1000 + 200
        try:
            livre_mb = shutil.disk_usage(pasta_destino).free / (1024 * 1024)
            if livre_mb < precisa_mb:
                emit({"type": "error", "fileId": file_id, "error": f"Espaço insuficiente na pasta de destino: são necessários cerca de "
                                                                    f"{precisa_mb / 1024:.1f} GB e há {livre_mb / 1024:.1f} GB livres.",
                      "code": "err_space", "args": [f"{precisa_mb / 1024:.1f}", f"{livre_mb / 1024:.1f}"]})
                return
        except OSError:
            pass
        nome_base = re.sub(r'[\\/*?:\'"<>|]', "", titulo).strip() or "Audiobook"
        # Pasta de trabalho: pelo título ou, se quem chamou mandar "cache_id" (ex.: um código do arquivo de origem),
        # por ele — assim mudar o título não perde o que já foi narrado
        nome_cache = str(file_info.get("cache_id") or titulo)
        cache_dir = os.path.join(pasta_destino, ".cache_audiobooks", re.sub(r'[\\/*?:\'"<>|]', "_", nome_cache).strip() or "audiobook")
        os.makedirs(cache_dir, exist_ok=True)
        trava = os.path.join(cache_dir, ".convertendo")
        dono = _ler_json(trava, {}).get("pid") if os.path.exists(trava) else None
        if dono and dono != os.getpid() and _processo_vivo(dono):
            emit({"type": "error", "fileId": file_id,
                  "error": "Este livro já está sendo convertido em outra janela do app. Espere aquela conversão terminar "
                           "(ou cancele-a) antes de converter de novo.",
                  "code": "err_busy",
                  "details": f"Pasta de trabalho: {cache_dir}\nProcesso que está convertendo: {dono}"})
            return
        _salvar_json(trava, {"pid": os.getpid()})
        _TRAVAS.append(trava)

        total = max(1, sum(len([p for p in dividir_em_blocos_seguros(f"{t}.\n\n{x}") if re.search(r"\w", p)]) for t, x in narracao))
        # Uma porcentagem só para o livro todo: leitura até 3%, tradução 3-15%, narração até 75%, volume dos capítulos
        # 75-85%, arquivo final 85-95% e texto sincronizado 95-99%. Ao lado, a contagem da etapa atual.
        teto = 75
        feitos = {"n": 0}
        caps_prontos = {"n": 0}
        n_caps = max(1, len(narracao))

        def emitir_volume():
            emit({"type": "progress", "fileId": file_id, "status": "processing",
                  "progress": round(teto + caps_prontos["n"] / n_caps * 10, 1), "message": "Ajustando o volume dos capítulos",
                  "code": "engine_volume", "partsDone": caps_prontos["n"], "partsTotal": n_caps,
                  "partsUnit": "capítulos com volume ajustado", "unitCode": "unit_volume"})

        def progresso(n):
            feitos["n"] += n
            if feitos["n"] >= total:  # narração toda pronta: agora o que falta é o volume dos capítulos
                emitir_volume()
                return
            emit({"type": "progress", "fileId": file_id, "status": "processing",
                  "progress": round(inicio_narracao + feitos["n"] / total * (teto - inicio_narracao), 1), "message": "Narrando",
                  "code": "engine_narrating", "partsDone": feitos["n"], "partsTotal": total,
                  "partsUnit": "partes narradas", "unitCode": "unit_narrated"})

        def capitulo_pronto():
            caps_prontos["n"] += 1
            if feitos["n"] >= total:
                emitir_volume()

        emit({"type": "progress", "fileId": file_id, "status": "processing", "progress": inicio_narracao, "message": "Narrando",
              "code": "engine_narrating", "partsDone": 0, "partsTotal": total, "partsUnit": "partes narradas", "unitCode": "unit_narrated"})

        async def medir_velocidade():
            """Velocidade real de download do áudio (média dos últimos segundos), enviada ao card."""
            anterior, t_ant = _BYTES.get(file_id, 0), asyncio.get_running_loop().time()
            amostras = []
            while True:
                await asyncio.sleep(1.5)
                agora, t = _BYTES.get(file_id, 0), asyncio.get_running_loop().time()
                amostras = (amostras + [(agora - anterior) / max(0.001, t - t_ant)])[-4:]
                anterior, t_ant = agora, t
                emit({"type": "speed", "fileId": file_id, "bytesPerSec": round(sum(amostras) / len(amostras))})

        await _mudar_narrando(file_id, True)
        medidor = asyncio.create_task(medir_velocidade())
        try:
            resultados = await asyncio.gather(*[
                montar_capitulo(i, tf, tx, voz, cache_dir, progresso, com_palavras, file_id, capitulo_pronto)
                for i, (tf, tx) in enumerate(narracao, start=1)])
        finally:
            medidor.cancel()
            await _mudar_narrando(file_id, False)
            emit({"type": "speed", "fileId": file_id, "bytesPerSec": 0})
        arquivos_cap = [r[0] for r in resultados]
        frases_cap = [r[1] for r in resultados]
        palavras_cap = [r[2] for r in resultados]

        # 5) Arquivo(s) final(is)
        gerados = []
        chapters_info = []
        nome_formato = ext_saida.lstrip(".").upper()

        def progresso_final(fracao, feitos_saida=0, total_saida=None, unidade=None, rotulo=None):
            # rotulo/unidade só são passados na saída por capítulo
            emit({"type": "progress", "fileId": file_id, "status": "processing", "progress": round(85 + fracao * 10, 1),
                  "message": rotulo or f"Montando o arquivo {nome_formato}",
                  "code": "engine_chapter_files" if rotulo else "engine_building", "args": [] if rotulo else [nome_formato],
                  "partsDone": feitos_saida, "partsTotal": total_saida or len(caps),
                  "partsUnit": unidade or f"capítulos no {nome_formato}",
                  "unitCode": "unit_chapter_files" if unidade else "unit_chapters_in", "unitArgs": [] if unidade else [nome_formato]})

        def progresso_montagem(fracao):
            # Quantos capítulos o FFmpeg já escreveu no arquivo final (pelo tempo já gerado)
            if chapters_info:
                gerado = fracao * chapters_info[-1]["end_time"]
                prontos = sum(1 for c in chapters_info if c["end_time"] <= gerado + 1)
                progresso_final(fracao, prontos, len(chapters_info))
            else:
                progresso_final(fracao)

        progresso_final(0, 0, len(caps), "arquivos de capítulo gerados" if is_multiple else None,
                        "Gerando os arquivos dos capítulos" if is_multiple else None)
        if is_multiple:
            pasta_livro = get_unique_path(os.path.join(pasta_destino, f"{nome_base} (Audiobook)"))
            os.makedirs(pasta_livro, exist_ok=True)

            async def um_capitulo(i, cap_file, cap):
                destino = get_unique_path(os.path.join(pasta_livro, f"{formatar_nome_capitulo(cap['titulo'], i, len(caps))}{ext_saida}"))
                async with _sem_ffmpeg:
                    await asyncio.to_thread(codificar_capitulo, cap_file, destino, fmt_saida, qualidade_saida)
                await asyncio.to_thread(aplicar_tags_capitulo_mp3, destino, cap["titulo"], autor, titulo, i, file_info.get("cover"), genero, narrador)
                return destino

            prontos_saida = {"n": 0}

            async def um_capitulo_com_progresso(i, f, c):
                destino = await um_capitulo(i, f, c)
                prontos_saida["n"] += 1
                progresso_final(prontos_saida["n"] / len(caps), prontos_saida["n"], len(caps), "arquivos de capítulo gerados",
                                "Gerando os arquivos dos capítulos")
                return destino

            gerados = list(await asyncio.gather(*[um_capitulo_com_progresso(i, f, c) for i, (f, c) in enumerate(zip(arquivos_cap, caps), start=1)]))
        else:
            inicio = 0
            for i, (cap_file, cap) in enumerate(zip(arquivos_cap, caps), start=1):
                dur = await asyncio.to_thread(duracao_ms, cap_file)
                if dur > 0:
                    chapters_info.append({"id": f"ch{i}", "title": cap["titulo"], "start_time": inicio, "end_time": inicio + dur, "file": cap_file})
                    inicio += dur
            os.makedirs(pasta_destino, exist_ok=True)
            saida = get_unique_path(os.path.join(pasta_destino, f"{nome_base}{ext_saida}"))
            await asyncio.to_thread(gerar_arquivo_unico, saida, fmt_saida, qualidade_saida, chapters_info, titulo, autor, genero, narrador,
                                    file_info.get("cover"), cache_dir, progresso_montagem)
            if ext_saida == ".mp3":
                await asyncio.to_thread(aplicar_tags_arquivo_unico, saida, titulo, autor, file_info.get("cover"), chapters_info, genero, narrador)
            gerados.append(saida)
        ano = str(file_info.get("year") or "").strip()
        if ano:
            for arquivo in gerados:
                if os.path.splitext(arquivo)[1].lower() in EXTENSOES_AUDIO:
                    await asyncio.to_thread(aplicar_ano, arquivo, ano)

        # 6) Texto sincronizado: os tempos de cada frase vêm da própria síntese (sem transcrever o áudio)
        sync_text_info = None
        if gerar_sync:
            emit({"type": "progress", "fileId": file_id, "status": "syncing", "progress": 95, "message": "Gerando o texto sincronizado",
                  "code": "engine_sync"})
            try:
                from sync_text_tools import save_sync_text
                destino_texto = _escolher(file_info, cfg, "sync_text_destination", padrao="both")
                audios = [p for p in gerados if os.path.splitext(p)[1].lower() in EXTENSOES_AUDIO]
                segmentos, lista_palavras, capitulos_sync, deslocamento = [], [], [], 0.0
                for i, (cap, frases, palavras, cap_file) in enumerate(zip(caps, frases_cap, palavras_cap, arquivos_cap)):
                    dur = (chapters_info[i]["end_time"] - chapters_info[i]["start_time"]) / 1000 if not is_multiple and i < len(chapters_info) else duracao_ms(cap_file) / 1000
                    if not is_multiple and i < len(chapters_info):
                        deslocamento = chapters_info[i]["start_time"] / 1000
                    capitulos_sync.append({"title": cap["titulo"], "start": round(deslocamento, 3), "end": round(deslocamento + dur, 3)})
                    for ini, fim, txt in frases:
                        segmentos.append({"start": round(deslocamento + ini, 3), "end": round(deslocamento + fim, 3), "text": txt, "chapter": i + 1})
                    for ini, fim, txt in palavras:
                        lista_palavras.append({"start": round(deslocamento + ini, 3), "end": round(deslocamento + fim, 3), "text": txt, "chapter": i + 1})
                    if is_multiple:
                        deslocamento += dur
                # Só palavras: o texto é dividido palavra por palavra; ambas: frases + palavras de cada frase
                if separacao == "words" and lista_palavras:
                    segmentos = []
                elif separacao == "sentences":
                    lista_palavras = []
                sync_data = {"audio": os.path.basename(audios[0]) if audios else titulo, "title": titulo, "author": autor,
                             "sync_source": "tts_word_timing" if lista_palavras else "tts_sentence_timing",
                             "granularity": separacao, "chapters": capitulos_sync, "segments": segmentos}
                if lista_palavras:
                    sync_data["words"] = lista_palavras
                resultado = await asyncio.to_thread(
                    save_sync_text, sync_data, audios[0] if audios else None,
                    os.path.dirname(audios[0]) if audios else pasta_destino, titulo, autor, destino_texto,
                    audios if not is_multiple else [], lambda m: emit({"type": "log", "message": m}))
                gerados.extend(resultado["files"])
                if resultado.get("fallbackToLibrary"):
                    emit({"type": "log", "message": "Este formato não guarda texto nos metadados: o texto foi salvo na pasta da biblioteca."})
                sync_text_info = {"content": resultado["content"], "format": resultado["format"], "wordCount": resultado["wordCount"],
                                  "files": resultado.get("files") or [],
                                  "savedToLibrary": resultado["savedToLibrary"], "embedded": len(resultado["embedded"]) > 0}
                emit({"type": "progress", "fileId": file_id, "status": "syncing", "progress": 99, "message": "Texto sincronizado pronto",
                      "code": "engine_sync_done"})
            except Exception as se:
                emit({"type": "log", "message": f"Aviso sincronização texto: {se}"})

        drive_folder_id = config.get("driveFolderId")
        if drive_folder_id and os.path.exists('token.json'):
            for f in gerados:
                await upload_to_drive_async(f, drive_folder_id, file_id, emit)

        tamanho = sum(os.path.getsize(p) for p in gerados if os.path.exists(p)) / (1024 * 1024)
        dur_total = sum(duracao_ms(p) for p in gerados if os.path.exists(p) and os.path.splitext(p)[1].lower() in EXTENSOES_AUDIO)
        s = int(dur_total / 1000)
        dur_str = (f"{s // 3600}h {(s % 3600) // 60}m" if s >= 3600 else f"{s // 60}m" if s >= 60 else f"{s}s") if s else ""
        emit({"type": "done", "fileId": file_id, "paths": gerados, "finalSize": f"{tamanho:.2f} MB", "finalDuration": dur_str, "syncText": sync_text_info})

        # Concluído: o cache deste livro não é mais necessário
        shutil.rmtree(cache_dir, ignore_errors=True)
        base_cache = os.path.dirname(cache_dir)
        if os.path.isdir(base_cache) and not os.listdir(base_cache):
            os.rmdir(base_cache)
    except (FalhaSintese, FalhaTraducao) as e:
        emit({"type": "error", "fileId": file_id, "error": str(e), "code": getattr(e, "codigo", None),
              "args": getattr(e, "args_codigo", []), "details": _detalhes_erro(file_id, e)})
    except Exception as e:
        import traceback
        rastreio = traceback.format_exc()
        emit({"type": "log", "message": f"Erro em '{file_info.get('title', '')}': {rastreio[-1500:]}"})
        etapa = _ETAPA.get(file_id, "início")
        emit({"type": "error", "fileId": file_id,
              "error": f"A conversão falhou na etapa de {etapa} ({type(e).__name__}): {e}",
              "code": "err_stage", "args": [_ETAPA_COD.get(file_id, "stage_start"), type(e).__name__, str(e)],
              "details": _detalhes_erro(file_id, e, rastreio)})


async def main():
    global _sem_tts, _sem_ffmpeg, _sem_traducao
    if len(sys.argv) < 2:
        print("Uso: python converter.py <config.json>")
        sys.exit(1)
    arg = sys.argv[1]
    if arg.endswith(".json") and os.path.exists(arg):
        with open(arg, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = json.loads(arg)
    global _vagas, TTS_CONEXOES, FFMPEG_PARALELO
    leve = bool(config.get("lite"))
    turbo = bool(config.get("turbo")) and not leve
    if leve:
        # Modo leve: um livro por vez, menos conexões de voz, um FFmpeg por vez e prioridade mínima.
        # Converte mais devagar, mas o computador e o app ficam livres.
        TTS_CONEXOES, FFMPEG_PARALELO = 4, 1
        _prioridade_baixa(minima=True)
    elif turbo:
        # Modo turbo: mais conexões de voz, um FFmpeg por núcleo (até 8) e prioridade normal. Usa bem mais
        # processador e memória; a transcrição por IA já usa a placa de vídeo (CUDA) quando existe.
        TTS_CONEXOES, FFMPEG_PARALELO = 12, max(2, min(8, os.cpu_count() or 2))
        _prioridade_baixa(normal=True)
    _sem_tts = asyncio.Semaphore(TTS_CONEXOES)
    _sem_ffmpeg = asyncio.Semaphore(FFMPEG_PARALELO)
    _vagas = asyncio.Condition()
    _sem_traducao = asyncio.Semaphore(1 if leve else 6 if turbo else 3)
    livros = config.get("files", [])
    try:
        if leve:
            for f in livros[1:]:
                emit({"type": "progress", "fileId": f.get("id"), "progress": 0, "message": "Na fila (modo leve: um livro por vez)",
                      "code": "queued_lite"})
            for f in livros:
                await process_file(f, config)
        else:
            await asyncio.gather(*[process_file(f, config) for f in livros])
    finally:
        for trava in _TRAVAS:
            try:
                os.remove(trava)
            except OSError:
                pass


def _proteger_contra_sinais():
    """Ctrl+C e o fechamento de janelas de console chegam a todos os processos do mesmo console no
    Windows e derrubavam a conversão sem aviso (código 0xC000013A). Quem encerra a conversão é o app
    (botão Cancelar, que finaliza o processo): os sinais do console são ignorados."""
    import signal
    for nome in ("SIGINT", "SIGBREAK"):
        sinal = getattr(signal, nome, None)
        if sinal is not None:
            try:
                signal.signal(sinal, signal.SIG_IGN)
            except (OSError, ValueError):
                pass


def _encerrar_se_o_app_fechar():
    """O servidor do app mantém a entrada deste processo aberta. Se ele fechar (app encerrado), a
    entrada termina e a conversão para também, em vez de ficar rodando sozinha. O que já foi narrado
    fica no cache e a conversão continua de onde parou na próxima vez."""
    import threading

    def vigiar():
        try:
            while sys.stdin.read(1024):
                pass
        except Exception:
            pass
        os._exit(1)

    if not sys.stdin or sys.stdin.isatty():
        return
    threading.Thread(target=vigiar, daemon=True).start()


def _prioridade_baixa(minima=False, normal=False):
    """A conversão roda com prioridade abaixo do normal: o app (carregar livros, capas) e o resto do
    computador continuam rápidos; a conversão usa a CPU que sobra, que costuma ser quase toda.
    No modo leve, prioridade mínima (só usa a CPU que ninguém está usando), também para o FFmpeg; no modo
    turbo, prioridade normal (disputa a CPU de igual para igual com os outros programas)."""
    global _CLASSE_PRIORIDADE
    try:
        if os.name == "nt":
            import ctypes
            _CLASSE_PRIORIDADE = 0x00000040 if minima else 0x00000020 if normal else 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), _CLASSE_PRIORIDADE)
        else:
            os.nice(10 if minima else 0 if normal else 5)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    _prioridade_baixa()
    _proteger_contra_sinais()
    _encerrar_se_o_app_fechar()
    asyncio.run(main())
