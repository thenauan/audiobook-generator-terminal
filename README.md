# Ebook to Audiobook CLI 📚🎧

Pipeline assíncrono e automatizado em Python para conversão de e-books e documentos digitais em audiobooks no formato MP3, utilizando síntese de voz neural (Edge TTS), segmentação estrutural por NLP, tradução automática contextual e metadados ID3 completos.

---

## 🚀 Principais Recursos

- **Amplo Suporte de Formatos:** Extração de texto para arquivos `.epub`, `.pdf`, `.mobi`, `.azw` e `.txt`.
- **Síntese de Voz Neural (Edge TTS):** Geração de áudio com vozes neurais de alta fidelidade em múltiplos idiomas (Português BR, Inglês, Espanhol e Árabe).
- **Processamento Assíncrono e em Lote:** Execução paralela com controle de concorrência (`asyncio.Semaphore`), filas de slots visuais e barras de progresso aninhadas com telemetria de taxa e tempo restante (`tqdm`, `colorama`).
- **Motor de Parsing por NLP:**
  - Reconhecimento inteligente de capítulos, prólogos, epílogos e partes por padrões numéricos, extensos e algarismos romanos.
  - Heurística para eliminação automática de cabeçalhos e rodapés repetitivos de páginas.
  - Normalização e otimização fonética de pontuações, travessões e abreviações em Português (ex.: *Sr.* → *Senhor*, *Dra.* → *Doutora*, *Cap.* → *Capítulo*).
- **Camada de Tradução Literária:** Detecção automática de idioma com suporte a tradução via Google Translator ou Google Gemini IA, com preservação de estilo e injeção de contexto prévio da narrativa.
- **Dois Modos de Saída de Áudio:**
  1. **Álbum / Pastas por Capítulo:** Cria arquivos `.mp3` individuais por capítulo, nomeados e ordenados.
  2. **Arquivo Único Monolítico:** Gera um único arquivo `.mp3` com tabelas de capítulos (`CHAP` e `CTOC`) incorporadas aos metadados ID3, permitindo navegação em players compatíveis.
- **Metadados Completos:** Marcação de tags ID3v2, incluindo título, autor, álbum e número de faixa, utilizando `mutagen`.
- **Sincronização com Nuvem:** Integração nativa para exportação simultânea ao Google Drive quando detectado localmente.

---

## 📁 Estrutura de Arquivos

```text
.
├── gerador_de_audiobook.py    # Pipeline principal
├── APP.bat                    # Script automatizado para Windows
├── requirements.txt           # Dependências do Python
├── .env                       # Configuração opcional da GEMINI_API_KEY
├── audiobook_generator.log    # Logs e telemetria
└── README.md
```

---

## 🛠️ Pré-requisitos e Dependências

O projeto requer **Python 3.8+** instalado com a opção *Add Python to PATH* habilitada.

### Principais Dependências

- `edge-tts` — Síntese de voz neural.
- `ebooklib` — Extração e processamento de arquivos EPUB.
- `pypdf` — Extração de texto de arquivos PDF.
- `mobi` — Processamento de arquivos MOBI.
- `beautifulsoup4` — Parsing e limpeza de HTML.
- `mutagen` — Metadados ID3 e capítulos incorporados.
- `tqdm` — Barras de progresso e telemetria.
- `colorama` — Formatação do terminal.
- `deep-translator` — Serviços de tradução.
- `langdetect` — Detecção automática de idioma.

---

## 💻 Instalação e Uso

### Modo Rápido — Windows

Execute:

```bat
APP.bat
```

O script verifica o interpretador Python, instala as dependências necessárias e inicializa a aplicação.

### Instalação Manual

#### 1. Clone o repositório

```bash
git clone https://github.com/thenauan/ebook-to-audiobook-cli.git
cd ebook-to-audiobook-cli
```

#### 2. Crie um ambiente virtual

**Linux / macOS:**

```bash
python -m venv venv
source venv/bin/activate
```

**Windows:**

```powershell
python -m venv venv
venv\Scripts\activate
```

#### 3. Instale as dependências

```bash
pip install -r requirements.txt
```

#### 4. Execute o programa

```bash
python gerador_de_audiobook.py
```

---

## ⚙️ Fluxo de Operação

O processo de conversão segue estas etapas:

1. **Seleção de Arquivos:** Janelas do sistema solicitam os arquivos de entrada e a pasta de saída.
2. **Auditoria de Metadados e Idioma:** O sistema detecta o idioma e permite configurar título, autor e tradução.
3. **Seleção de Narrador:** Escolha da voz neural utilizada na síntese.
4. **Relatório Preditivo:** Estimativa da duração do áudio, tempo de conversão e consumo de banda.
5. **Extração e Processamento:** O conteúdo é extraído, limpo e segmentado.
6. **Tradução:** Caso habilitada, a obra é traduzida mantendo o contexto da narrativa.
7. **Síntese de Voz:** Os segmentos são convertidos em áudio de maneira assíncrona.
8. **Montagem:** Os arquivos são organizados conforme o modo de saída escolhido.
9. **Metadados:** Informações da obra e capítulos são incorporados aos arquivos MP3.
10. **Exportação:** Os arquivos finais são armazenados localmente e, quando disponível, podem ser exportados para o Google Drive.

Os registros detalhados ficam em:

```text
audiobook_generator.log
```

---

## 🔑 Configuração da API do Gemini (Opcional)

O Gemini pode ser utilizado para traduzir obras mantendo melhor o contexto e o estilo literário da narrativa.

### 1. Obtenha uma chave de API

A chave pode ser obtida pelo Google AI Studio:

https://aistudio.google.com/app/apikey

### 2. Configure a chave

O aplicativo pode solicitar a chave na primeira execução e criar automaticamente o arquivo `.env`.

Também é possível configurá-la manualmente:

```env
GEMINI_API_KEY=sua_chave_aqui
```

> **Importante:** nunca compartilhe ou versione o arquivo `.env` contendo sua chave de API.

Recomenda-se adicionar `.env` ao `.gitignore`:

```gitignore
.env
```

---

## 🎧 Modos de Saída

### Álbum / Capítulos Separados

Cada capítulo é convertido em um arquivo MP3 independente:

```text
output/
├── 01 - Prólogo.mp3
├── 02 - Capítulo 1.mp3
├── 03 - Capítulo 2.mp3
├── 04 - Capítulo 3.mp3
└── ...
```

Esse modo facilita a organização e a compatibilidade com players convencionais.

### Arquivo Único

Toda a obra é combinada em um único arquivo MP3.

Os capítulos são incorporados aos metadados utilizando estruturas `CHAP` e `CTOC`, permitindo que players compatíveis exibam uma estrutura navegável de capítulos.

---

## 🧠 Processamento de Texto

O pipeline possui uma camada de processamento destinada a transformar o texto original em uma entrada adequada para síntese de voz.

Entre os tratamentos realizados estão:

- Identificação de capítulos e seções.
- Reconhecimento de numeração romana e arábica.
- Identificação de prólogos e epílogos.
- Remoção de cabeçalhos e rodapés repetitivos.
- Limpeza de elementos desnecessários.
- Normalização de pontuação.
- Tratamento de travessões e diálogos.
- Expansão de abreviações.
- Otimização do texto para pronúncia natural.

Exemplos:

```text
Sr.  → Senhor
Sra. → Senhora
Dra. → Doutora
Dr.  → Doutor
Cap. → Capítulo
```

---

## 🌎 Tradução

O sistema pode detectar automaticamente o idioma do documento e oferecer tradução para o idioma selecionado.

As opções incluem:

- Google Translator
- Google Gemini

O modo baseado em Gemini pode utilizar contexto de trechos anteriores da narrativa para reduzir inconsistências de tradução, especialmente em:

- Nomes próprios.
- Personagens.
- Termos recorrentes.
- Estilo narrativo.
- Contexto de diálogos.

---

## 📊 Processamento Assíncrono

A geração dos audiobooks utiliza processamento assíncrono para acelerar a conversão de documentos extensos.

O número de tarefas simultâneas é controlado para evitar sobrecarga do sistema e do serviço de síntese de voz.

A execução apresenta informações como:

- Progresso atual.
- Taxa de processamento.
- Tempo decorrido.
- Tempo restante estimado.
- Quantidade de segmentos processados.
- Status das tarefas.

---

## 📝 Logs

Informações detalhadas sobre a execução são registradas em:

```text
audiobook_generator.log
```

O arquivo pode ser utilizado para diagnosticar problemas durante:

- Extração.
- Tradução.
- Síntese de voz.
- Montagem dos arquivos.
- Inserção de metadados.
- Exportação.

---

## ⚠️ Observações

- É necessária uma conexão com a internet para utilizar a síntese de voz do Edge TTS.
- A tradução via Gemini requer uma chave de API válida.
- O tempo de conversão depende do tamanho do documento, da quantidade de segmentos e da velocidade de processamento disponível.
- O suporte a determinados formatos pode depender das ferramentas externas utilizadas para sua extração.
- Certifique-se de possuir os direitos necessários para converter e distribuir os livros utilizados.

---

## 📄 Licença

Este projeto ainda não possui uma licença definida.

Se você pretende disponibilizar o projeto publicamente, recomenda-se adicionar uma licença apropriada, como **MIT**, **GPL-3.0** ou outra que corresponda aos objetivos do projeto.