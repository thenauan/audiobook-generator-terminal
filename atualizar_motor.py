"""Copia o motor de conversão e o catálogo de vozes do app (pasta audiobook-generatior ao lado desta)
para cá, para a versão de terminal converter exatamente como o app. Rode depois de mudar o motor do app:
    python atualizar_motor.py [caminho-do-app]
"""
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
APP = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(BASE), "audiobook-generatior")
MODULOS = ["converter.py", "audio_output.py", "book_structure.py", "extract_metadata.py", "sync_text_tools.py",
           "ebook_audiobook_converter.py", "capa_generica.py", "texto_falado.py", "emojis_fala.json"]

if not os.path.isfile(os.path.join(APP, "converter.py")):
    sys.exit(f"App não encontrado em {APP}")
for m in MODULOS:
    shutil.copy2(os.path.join(APP, m), os.path.join(BASE, "motor", m))
    print(f"✔ motor/{m}")
shutil.copy2(os.path.join(APP, "src", "data", "edgeVoices.json"), os.path.join(BASE, "dados", "vozes.json"))
print("✔ dados/vozes.json")
print("Motor atualizado. (Se o catálogo de vozes ganhou idiomas novos, gere de novo dados/idiomas.json.)")
