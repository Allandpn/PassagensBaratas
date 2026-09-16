# -*- coding: utf-8 -*-
"""Teste offline: janela de datas, bases de preco, faixas, anti-spam e
montagem da mensagem. Nao usa rede."""
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


def oferta_vdp(preco=966, tarifa=966, com_taxas=1148, total=1245, volta="2026-11-23"):
    return m.Oferta(fonte=m.VAIDEPROMO, preco=preco, tarifa=tarifa, com_taxas=com_taxas,
                    total=total, companhia="LATAM", rota="FLN-GRU-SDU", partida="31/10 15:40",
                    chegada="31/10 22:25", paradas=1, destino="SDU",
                    data_ida="2026-10-31", data_volta=volta)


print("Janela de datas:")
check("data única (string)", m.expandir_datas("2026-11-22"), ["2026-11-22"])
check("lista explícita", m.expandir_datas(["2026-11-24", "2026-11-22"]),
      ["2026-11-22", "2026-11-24"])
check("intervalo de 22 a 28", len(m.expandir_datas({"de": "2026-11-22", "ate": "2026-11-28"})), 7)
check("intervalo de 1 dia", m.expandir_datas({"de": "2026-11-22"}), ["2026-11-22"])
try:
    m.expandir_datas({"de": "2026-11-28", "ate": "2026-11-22"})
    check("intervalo invertido é rejeitado", False, True)
except ValueError:
    check("intervalo invertido é rejeitado", True, True)

print("\nCombinações:")
check("config atual (1 ida x 7 voltas)", len(m.combinacoes(CFG)), 7)
check("primeira combinação", m.combinacoes(CFG)[0], ("2026-10-31", "2026-11-22"))
check("última combinação", m.combinacoes(CFG)[-1], ("2026-10-31", "2026-11-28"))
check("volta anterior à ida é descartada",
      m.combinacoes({**CFG, "data_ida": "2026-11-25",
                     "data_volta": {"de": "2026-11-22", "ate": "2026-11-28"}}),
      [("2026-11-25", "2026-11-25"), ("2026-11-25", "2026-11-26"),
       ("2026-11-25", "2026-11-27"), ("2026-11-25", "2026-11-28")])
check("teto de combinações corta", len(m.combinacoes({**CFG, "max_combinacoes_datas": 3})), 3)
check("ida também pode ser janela",
      len(m.combinacoes({**CFG, "data_ida": {"de": "2026-10-30", "ate": "2026-10-31"},
                         "max_combinacoes_datas": 99})), 14)

print("\nBases de preço (tarifa 966 / com taxas 1148 / total 1245):")
for base, esperado in [("tarifa", 966), ("com_taxas", 1148), ("total", 1245)]:
    check(base, f.escolher_base({"base_preco": base}, 966, 1148, 1245), esperado)
check("base desconhecida cai no padrão",
      f.escolher_base({"base_preco": "inventada"}, 966, 1148, 1245), 1148)
check("sem base configurada usa o padrão", f.escolher_base({}, 966, 1148, 1245), 1148)

print("\nDecomposição na mensagem:")
linha_vdp = oferta_vdp().linha()
check("Vai de Promo mostra os três valores",
      all(t in linha_vdp for t in ("tarifa R$ 966", "c/ taxas R$ 1.148", "total R$ 1.245")), True)
linha_google = m.Oferta(fonte=m.GOOGLE, preco=1255, tarifa=1255, com_taxas=1255, total=1255,
                        companhia="Gol", rota="FLN-GIG", partida="31/10 17:30",
                        chegada="31/10 19:00", paradas=0, destino="GIG",
                        data_ida="2026-10-31", data_volta="2026-11-22").linha()
check("Google omite a decomposição (valores iguais)", "tarifa R$" in linha_google, False)

print("\nFaixas:")
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
res = [
    m.Resultado(fonte=m.VAIDEPROMO, destino="SDU", data_ida="2026-10-31",
                data_volta="2026-11-23", piso=966, ofertas=[oferta_vdp()], url="https://vdp"),
    m.Resultado(fonte=m.GOOGLE, destino="GIG", data_ida="2026-10-31",
                data_volta="2026-11-22", piso=1255,
                ofertas=[m.Oferta(fonte=m.GOOGLE, preco=1255, tarifa=1255, com_taxas=1255,
                                  total=1255, companhia="Gol", rota="FLN-GIG",
                                  partida="31/10 17:30", chegada="31/10 19:00", paradas=0,
                                  destino="GIG", data_ida="2026-10-31",
                                  data_volta="2026-11-22")],
                url="https://google"),
]
for faixa in ("jackpot", "alerta", "aviso", "acima"):
    texto = m.montar_mensagem(CFG, res, faixa, 966, "teste", resumo=False)
    check(f"faixa {faixa} renderiza", bool(texto) and "{" not in texto.split("\n")[0], True)
    print("      " + m._sem_tags(texto).split("\n")[0])

msg = m._sem_tags(m.montar_mensagem(CFG, res, "alerta", 966, "teste", resumo=False))
check("mensagem anuncia a janela de volta", "volta 22/11 a 28/11 (7 datas)" in msg, True)
check("mensagem diz qual base dispara o alerta", "tarifa por adulto" in msg, True)
check("mensagem destaca a melhor combinação", "31/10 → 23/11" in msg, True)
check("mensagem mostra o total junto da tarifa", "total R$ 1.245" in msg, True)
check("mensagem cabe no limite do Telegram (4096)", len(msg) < 4096, True)
check("piso por data de volta", m.pisos_por_volta(res),
      {"2026-11-22": 1255, "2026-11-23": 966})
check("sem oferta nenhuma ainda renderiza",
      "Nenhuma oferta retornada" in m._sem_tags(
          m.montar_mensagem(CFG, [], "acima", None, "teste", resumo=False)), True)

print("\nHistorico (aceita formato antigo e novo):")
check("formato antigo", m.pisos_validos([{"pisos": {"RIO": 1306, "GIG": None}}]), [1306])
check("formato novo", sorted(m.pisos_validos(
    [{"pisos": {"Google Flights": {"RIO 22/11": 1306}, "Vai de Promo": {"GIG 23/11": 1665}}}])),
    [1306, 1665])
check("rótulo do resultado inclui a data de volta", res[0].rotulo, "SDU 23/11")

print("\nLinks das fontes:")
url_google = f._query_google(CFG, "RIO", "2026-10-31", "2026-11-28", None).url()
check("google", url_google.startswith("https://www.google.com/travel/flights/search?tfs="), True)
print("      " + url_google)

check("token vaidepromo ida e volta",
      f._token_vdp(CFG, "GIG", "2026-10-31", "2026-11-22"), "FLNGIG261031-GIGFLN261122")
check("token acompanha a data da janela",
      f._token_vdp(CFG, "GIG", "2026-10-31", "2026-11-28"), "FLNGIG261031-GIGFLN261128")
url_vdp = f.VDP_PAGINA.format(token=f._token_vdp(CFG, "GIG", "2026-10-31", "2026-11-23"),
                              ad=CFG["adultos"])
check("vaidepromo", url_vdp.startswith("https://www.vaidepromo.com.br/passagens-aereas/"), True)
print("      " + url_vdp)

print("\nDedup de companhias do Vai de Promo:")
providers = [
    {"code": "AD", "endpoint": "https://flights.vaidepromo.com.br"},
    {"code": "G3", "endpoint": "https://flights.vaidepromo.com.br"},
    {"code": "LA", "endpoint": "https://flights.vaidepromo.com.br"},
    {"code": "I8", "endpoint": "https://i8.flights.vaidepromo.com.br", "mileage_carrier": "AD"},
    {"code": "I8", "endpoint": "https://i8.flights.vaidepromo.com.br", "mileage_carrier": "G3"},
    {"code": "I8", "endpoint": "https://i8.flights.vaidepromo.com.br", "mileage_carrier": "LA"},
    {"code": "WB", "endpoint": "https://wb.flights.vaidepromo.com.br", "mileage_carrier": "AD"},
    {"code": "WB", "endpoint": "https://wb.flights.vaidepromo.com.br", "mileage_carrier": "LA"},
    {"code": "TS", "endpoint": "https://ts.flights.vaidepromo.com.br", "mileage_carrier": "G3"},
    {"code": "TS", "endpoint": "https://ts.flights.vaidepromo.com.br", "mileage_carrier": "LA"},
    {"code": "XX", "endpoint": None},
]
urls = f.urls_de_busca(CFG, providers, "FLNGIG261031-GIGFLN261123")
check("10 entradas viram 6 buscas", len(urls), 6)
check("sem endpoint é ignorado", "XX" in urls, False)

print("\n" + ("TUDO OK" if not falhas else "FALHAS:\n  " + "\n  ".join(falhas)))
sys.exit(1 if falhas else 0)
