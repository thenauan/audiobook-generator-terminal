@echo off
title Gerador de Audiobook Pro
color 0B

:: 1. Verifica se o Python esta no PATH
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO FATAL] Python nao encontrado neste computador!
    echo Por favor, instale o Python e marque a caixa "Add Python to PATH" durante a instalacao.
    pause
    exit /b
)

:: 2. Verifica se a instalacao ja foi feita
if exist .dependencias_ok goto EXECUTA_APP

echo ===================================================
echo   Primeira execucao: Instalando dependencias...
echo ===================================================

set PACOTES=edge-tts tqdm colorama ebooklib mobi beautifulsoup4 pypdf deep-translator langdetect mutagen

for %%p in (%PACOTES%) do (
    pip show %%p >nul 2>&1
    if errorlevel 1 (
        echo [+] Instalando %%p...
        pip install %%p -q
    )
)
echo.
echo [OK] Tudo pronto! Criando arquivo de confirmacao...
echo OK > .dependencias_ok
timeout /t 2 >nul

:EXECUTA_APP
:: 3. Roda o sistema principal
cls
python gerador_de_audiobook.py

:: 4. Mantem a janela aberta em caso de erro critico
pause