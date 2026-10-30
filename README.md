# Audiobook Generator — versão de terminal

Converte e-books (**EPUB, PDF, MOBI, AZW3, TXT**) em audiobooks (**M4B, MP3, Opus, FLAC, WAV**) direto no terminal — no **Windows, macOS e Linux** — com o **mesmo motor de conversão** do [Audiobook Generator](https://github.com/thenauan/audiobook-generator): capítulos, capa, vozes neurais da Microsoft (313 vozes, 73 idiomas), tradução e texto sincronizado para audiobook players.

```
  ◆ Etapa 6/6  🚀  Convertendo
  ─────────────────────────────────────────────────────────────────────────────
  ✔ Dom Casmurro               Concluído
  ● O Alienista                ━━━━━━━━━━━━━━━━━━━━━━━━  42.5%  Narrando · 3m 10s · 14/32 partes
  ● Memórias Póstumas de Br…   ━━━━━━━━━━━━━━━━━━━━━━━━   0.0%  Na fila · calculando
```

## Download

Baixe a versão mais recente em **[Releases](https://github.com/thenauan/audiobook-generator-terminal/releases/latest)** (`.zip` para Windows, `.tar.gz` para macOS e Linux) ou clone o repositório:

```bash
git clone https://github.com/thenauan/audiobook-generator-terminal.git
cd audiobook-generator-terminal
```

## Instalação

Precisa do **Python 3.10+** e do **FFmpeg** (o FFmpeg junta os capítulos e gera o arquivo final, em qualquer formato).

| Sistema | Python e FFmpeg | Bibliotecas do Python |
|---|---|---|
| **Windows** | [python.org](https://www.python.org/downloads/) (marque *Add Python to PATH*) e `winget install Gyan.FFmpeg` | `python -m pip install -r requirements.txt` |
| **macOS** | `brew install python python-tk ffmpeg` | `python3 -m pip install -r requirements.txt` |
| **Linux** (Debian/Ubuntu) | `sudo apt install python3 python3-pip python3-tk ffmpeg` | `python3 -m pip install -r requirements.txt` |
| **Linux** (Fedora) | `sudo dnf install python3 python3-pip python3-tkinter ffmpeg` | `python3 -m pip install -r requirements.txt` |
| **Linux** (Arch) | `sudo pacman -S python python-pip tk ffmpeg` | `python3 -m pip install -r requirements.txt` |

> Em distribuições que bloqueiam o `pip` do sistema (erro *externally-managed-environment*), use um ambiente virtual:
> `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`, e rode o app com o `.venv` ativado.

O `tk` (tkinter) é opcional: com ele, os livros e a pasta são escolhidos numa janela do sistema; sem ele (ou num servidor via SSH), basta digitar ou arrastar os arquivos para o terminal.

## Uso

| Sistema | Como abrir |
|---|---|
| **Windows** | dois cliques em `Audiobook Generator (terminal).bat` |
| **macOS** | dois cliques em `Audiobook Generator (terminal).command`, ou `./audiobook-cli.sh` no Terminal |
| **Linux** | `./audiobook-cli.sh` |
| **Qualquer um** | `python audiobook_cli.py` (ou `python3`) |

Também dá para passar os livros e a pasta na linha de comando:

```bash
./audiobook-cli.sh livro.epub outro.pdf -o ~/Audiobooks           # macOS / Linux
python audiobook_cli.py livro.epub outro.pdf -o "C:\Audiobooks"   # Windows
```

> **macOS:** na primeira vez, o Gatekeeper pode bloquear o `.command` baixado da internet. Clique com o botão direito → **Abrir**, ou rode `xattr -d com.apple.quarantine *.command *.sh` na pasta.
> Se aparecer *permission denied*, rode `chmod +x audiobook-cli.sh "Audiobook Generator (terminal).command"`.

## Como funciona

| Etapa | O que acontece |
|---|---|
| 1. E-books | Abre o seletor de arquivos (ou use os arquivos da linha de comando, ou arraste-os para o terminal). Título, autor, gênero, subgênero, ano, idioma e capítulos são lidos automaticamente. |
| 2. Cada livro | Mostra o que foi identificado e pergunta se quer editar. Depois: **traduzir** (para português, inglês, espanhol, árabe ou russo), **texto sincronizado** (sim/não) e **voz** (`o 3` toca a voz 3 lendo um trecho do próprio livro). Dá para repetir as escolhas nos outros livros do mesmo idioma. |
| 3. Formato | M4B (padrão), MP3, MP3 por capítulo, Opus, FLAC ou WAV. |
| 4. Pasta | Abre o seletor de pastas (padrão: `Documentos/Audiobook Generator`). |
| 5. Resumo | Confere tudo antes de começar. |
| 6. Conversão | Uma barra por livro, com etapa, tempo restante, partes narradas e velocidade real. |

A capa vem do próprio e-book e os capítulos são detectados sozinhos — não há edição de capa nem de metadados avançados, para ir direto ao ponto.

### Cancelar e continuar depois

Durante a conversão, **Ctrl+C** pausa o painel e pergunta se quer cancelar:

- **n** (ou Enter) — a conversão continua de onde estava;
- **s** — cancela e encerra o motor e o FFmpeg (nenhum processo fica rodando para trás);
- **Ctrl+C de novo** — confirma o cancelamento.

O que já foi narrado fica salvo em `.cache_audiobooks` dentro da pasta de destino: converta o mesmo livro de novo, com a mesma voz e a mesma pasta, e ele continua de onde parou.

## Cores e ícones

| Cor | Uso | | Ícone | Uso |
|---|---|---|---|---|
| ciano | etapas, números e teclas | | ◆ | etapa |
| branco | dados | | › | pergunta |
| cinza | dicas | | ✔ / ✖ / ▲ | concluído / erro / aviso |
| verde | sucesso | | 📚 🎙 🌐 📝 💾 🎧 | livros, voz, idioma, texto, pasta, formato |
| amarelo | avisos | | | |
| vermelho | erros | | | |

No console antigo do Windows e no console de texto do Linux (sem emojis), os ícones viram símbolos simples. Para desligar as cores: `NO_COLOR=1`.

## Requisitos

- Python 3.10 ou mais novo
- FFmpeg no `PATH`
- Internet (as vozes neurais e a tradução são serviços online)

## Manutenção

O motor (`motor/`) é uma cópia do motor do app. Depois de mudar o app, atualize com:

```bash
python atualizar_motor.py [caminho-do-app]
```
