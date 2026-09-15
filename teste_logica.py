# -*- coding: utf-8 -*-
"""Teste offline: faixas, anti-spam e montagem da mensagem. Nao usa rede."""
import json
import sys
from pathlib import Path

import fontes as f
import monitor as m

CFG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
falhas = []


def check(nome, obtido, esperado):
    ok = obtido == esperado
    print(f"  {'OK  ' if ok else 'FALHA'} {nome}: {obtido!r}")
    if not ok:
        falhas.append(f"{nome}: esperado {esperado!r}, veio {obtido!r}")


print("Faixas:")
for preco, esperado in [(650, "jackpot"), (799, "jackpot"), (800, "alerta"), (899, "alerta"),
                        (900, "aviso"), (999, "aviso"), (1000, "acima"), (1306, "acima")]:
    check(f"R$ {preco}", m.classificar(preco, CFG["alvos"]), esperado)

print("\nAnti-spam:")
m.ULTIMO_ALERTA = Path(__file__).parent / "state" / "_teste_alerta.json"
m.ULTIMO_ALERTA.unlink(missing_ok=True)
check("acima do alvo nao alerta", m.deve_alertar(1306, "acima", CFG)[0], False)
check("primeiro alerta", m.deve_alertar(950, "aviso", CFG)[0], True)
m.gravar_ultimo_alerta(950, "aviso")
check("mesmo preco repetido", m.deve_alertar(950, "aviso", CFG)[0], False)
check("preco caiu", m.deve_alertar(820, "alerta", CFG)[0], True)
check("preco subiu dentro do alvo", m.deve_alertar(980, "aviso", CFG)[0], False)
m.ULTIMO_ALERTA.unlink(missing_ok=True)

print("\nMensagens (as 4 faixas):")
oferta = m.Oferta(fonte=m.GOOGLE, preco=780, companhia="Gol", rota="FLN-GRU-SDU", partida="31/10 06:30",
                  chegada="31/10 09:45", paradas=1, destino="RIO")
res = [m.Resultado(fonte=m.GOOGLE, destino="RIO", piso=780, ofertas=[oferta], url="https://x")]
for faixa in ("jackpot", "alerta", "aviso", "acima"):
    texto = m.montar_mensagem(CFG, res, faixa, 780, "teste", resumo=False)
    check(f"faixa {faixa} renderiza", bool(texto) and "{" not in texto.split("\n")[0], True)
    print("      " + m._sem_tags(texto).split("\n")[0])

print("\nHistorico (aceita formato antigo e novo):")
check("formato antigo", m.pisos_validos([{"pisos": {"RIO": 1306, "GIG": None}}]), [1306])
check("formato novo", sorted(m.pisos_validos(
    [{"pisos": {"Google Flights": {"RIO": 1306}, "Vai de Promo": {"GIG": 1665}}}])), [1306, 1665])

print("\nLinks das fontes:")
url_google = f._query_google(CFG, "RIO", None).url()
check("google", url_google.startswith("https://www.google.com/travel/flights/search?tfs="), True)
print("      " + url_google)

check("token vaidepromo ida e volta", f._token_vdp(CFG, "GIG"), "FLNGIG261031-GIGFLN261122")
url_vdp = f.VDP_PAGINA.format(token=f._token_vdp(CFG, "GIG"), ad=CFG["adultos"])
check("vaidepromo", url_vdp.startswith("https://www.vaidepromo.com.br/passagens-aereas/"), True)
print("      " + url_vdp)

print("\n" + ("TUDO OK" if not falhas else "FALHAS:\n  " + "\n  ".join(falhas)))
sys.exit(1 if falhas else 0)
