# -*- coding: utf-8 -*-
"""Teste offline: janela de datas, bases de preco, faixas, anti-spam e
montagem da mensagem. Nao usa rede."""
import json
import os
import sys
from pathlib import Path

import fontes as f
import monitor as m

CFG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
falhas = []

# Base de configuração limpa para injetarmos os períodos dinamicamente nos testes
CFG_BASE = {k: v for k, v in CFG.items() if k not in ("data_ida", "data_volta", "periodos")}

# Configuração padrão de teste simulando 1 ida x 7 voltas
cfg_1x7 = {
    **CFG_BASE,
    "periodos": [{"ida": "2026-10-31", "volta": {"de": "2026-11-22", "ate": "2026-11-28"}}]
}

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
check("config 1 ida x 7 voltas", len(m.combinacoes(cfg_1x7)), 7)
check("primeira combinação", m.combinacoes(cfg_1x7)[0], ("2026-10-31", "2026-11-22"))
check("última combinação", m.combinacoes(cfg_1x7)[-1], ("2026-10-31", "2026-11-28"))

check("volta anterior à ida é descartada",
      m.combinacoes({**CFG_BASE, "periodos": [{"ida": "2026-11-25", "volta": {"de": "2026-11-22", "ate": "2026-11-28"}}]}),
      [("2026-11-25", "2026-11-25"), ("2026-11-25", "2026-11-26"),
       ("2026-11-25", "2026-11-27"), ("2026-11-25", "2026-11-28")])

check("teto de combinações corta", len(m.combinacoes({**cfg_1x7, "max_combinacoes_datas": 3})), 3)

check("ida também pode ser janela",
      len(m.combinacoes({**CFG_BASE, "periodos": [{"ida": {"de": "2026-10-30", "ate": "2026-10-31"},
                                                   "volta": {"de": "2026-11-22", "ate": "2026-11-28"}}],
                         "max_combinacoes_datas": 99})), 14)

check("múltiplos períodos são somados",
      len(m.combinacoes({**CFG_BASE, "periodos": [
          {"ida": "2026-10-31", "volta": "2026-11-22"},
          {"ida": "2026-12-20", "volta": "2026-12-25"}
      ]})), 2)

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
# Alvos fixos do teste: nao usar cfg_1x7["alvos"] aqui, pois vem do config.json
# de producao e muda com o destino monitorado (ja causou falsa falha quando o
# config trocou de Rio para Piaui).
ALVOS_TESTE = {"jackpot": 800, "alerta": 900, "aviso": 1000}
for preco, esperado in [(650, "jackpot"), (799, "jackpot"), (800, "alerta"), (899, "alerta"),
                        (900, "aviso"), (999, "aviso"), (1000, "acima"), (1306, "acima")]:
    check(f"R$ {preco}", m.classificar(preco, ALVOS_TESTE), esperado)

print("\nAnti-spam:")
m.ULTIMO_ALERTA = Path(__file__).parent / "state" / "_teste_alerta.json"
m.ULTIMO_ALERTA.unlink(missing_ok=True)
check("acima do alvo nao alerta", m.deve_alertar(1306, "acima", cfg_1x7)[0], False)
check("primeiro alerta", m.deve_alertar(950, "aviso", cfg_1x7)[0], True)
m.gravar_ultimo_alerta(950, "aviso")
check("mesmo preco repetido", m.deve_alertar(950, "aviso", cfg_1x7)[0], False)
check("preco caiu", m.deve_alertar(820, "alerta", cfg_1x7)[0], True)
check("preco subiu dentro do alvo", m.deve_alertar(980, "aviso", cfg_1x7)[0], False)
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
    texto = m.montar_mensagem(cfg_1x7, res, faixa, 966, "teste", resumo=False)
    check(f"faixa {faixa} renderiza", bool(texto) and "{" not in texto.split("\n")[0], True)
    print("      " + m._sem_tags(texto).split("\n")[0])

msg = m._sem_tags(m.montar_mensagem(cfg_1x7, res, "alerta", 966, "teste", resumo=False))
check("mensagem anuncia a janela de volta", "volta 22/11 a 28/11 (7 datas)" in msg, True)
check("mensagem diz qual base dispara o alerta", "tarifa por adulto" in msg, True)
check("mensagem destaca a melhor combinação", "31/10 → 23/11" in msg, True)
check("mensagem mostra o total junto da tarifa", "total R$ 1.245" in msg, True)
check("mensagem cabe no limite do Telegram (4096)", len(msg) < 4096, True)
check("piso por data de volta", m.pisos_por_volta(res),
      {"2026-11-22": 1255, "2026-11-23": 966})
check("sem oferta nenhuma ainda renderiza",
      "Nenhuma oferta retornada" in m._sem_tags(
          m.montar_mensagem(cfg_1x7, [], "acima", None, "teste", resumo=False)), True)

cfg_multi = {**CFG_BASE, "periodos": [{"ida": "2026-10-31", "volta": "2026-11-22"}, {"ida": "2026-12-20", "volta": "2026-12-25"}]}
msg_multi = m._sem_tags(m.montar_mensagem(cfg_multi, res, "alerta", 966, "teste", resumo=False))
check("mensagem agrega múltiplos períodos", "2 combinações em 2 períodos" in msg_multi, True)

print("\nHistorico (aceita formato antigo e novo):")
check("formato antigo", m.pisos_validos([{"pisos": {"RIO": 1306, "GIG": None}}]), [1306])
check("formato novo", sorted(m.pisos_validos(
    [{"pisos": {"Google Flights": {"RIO 22/11": 1306}, "Vai de Promo": {"GIG 23/11": 1665}}}])),
    [1306, 1665])
check("rótulo do resultado inclui a data de volta", res[0].rotulo, "SDU 23/11")

print("\nLinks das fontes:")
url_google = f._query_google(cfg_1x7, "RIO", "2026-10-31", "2026-11-28", None).url()
check("google", url_google.startswith("https://www.google.com/travel/flights/search?tfs="), True)
print("      " + url_google)

check("token vaidepromo ida e volta",
      f._token_vdp(cfg_1x7, "GIG", "2026-10-31", "2026-11-22"), "FLNGIG261031-GIGFLN261122")
check("token acompanha a data da janela",
      f._token_vdp(cfg_1x7, "GIG", "2026-10-31", "2026-11-28"), "FLNGIG261031-GIGFLN261128")
url_vdp = f.VDP_PAGINA.format(token=f._token_vdp(cfg_1x7, "GIG", "2026-10-31", "2026-11-23"),
                              ad=cfg_1x7["adultos"])
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
urls = f.urls_de_busca(cfg_1x7, providers, "FLNGIG261031-GIGFLN261123")
check("10 entradas viram 6 buscas", len(urls), 6)
check("sem endpoint é ignorado", "XX" in urls, False)

print("\nPassagens Promo (ida + volta somadas):")


def perna_pp(total, price, tax, fonte=48, cia="LA", nome="LATAM AIRLINES ", ori="FLN", des="SDU"):
    ponto = lambda iata, dia, h: {"iata": iata, "date": {"year": 2026, "month": 10, "day": dia},
                                  "time": {"hour": h, "minute": 5}}
    return {"key": f"{cia}-{total}", "source": fonte,
            "price": {"total": total, "price": price, "tax": tax},
            "journey": {"numberOfStops": 1, "marketingAirline": {"iata": cia, "name": nome},
                        "departure": ponto(ori, 31, 11), "destination": ponto(des, 31, 20),
                        "segments": [{"destination": {"iata": "GRU"}},
                                     {"destination": {"iata": des}}]}}


ida_pp = perna_pp(560.81, 425.89, 134.92)
volta_pp = perna_pp(950.82, 879.9, 70.92, ori="SDU", des="FLN")
oferta_pp = f._oferta_pp({"base_preco": "tarifa"}, ida_pp, volta_pp, "RIO",
                         "2026-10-31", "2026-11-22")
check("tarifa soma as duas pernas", oferta_pp.tarifa, round(425.89 + 879.9))
check("com taxas soma tarifa + embarque", oferta_pp.com_taxas, round(425.89 + 134.92 + 879.9 + 70.92))
check("total soma os totais", oferta_pp.total, round(560.81 + 950.82))
check("preco segue a base_preco", oferta_pp.preco, oferta_pp.tarifa)
check("rota da ida", oferta_pp.rota, "FLN-GRU-SDU")
check("companhia sem espaço sobrando", oferta_pp.companhia, "Latam Airlines")
check("destino é o aeroporto real, não o código de cidade", oferta_pp.destino, "SDU")
check("horário de partida", oferta_pp.partida, "31/10 11:05")

idas = [perna_pp(560.81, 1, 1), perna_pp(600, 1, 1),                    # LATAM duas vezes
        perna_pp(577.86, 1, 1, fonte=42, cia="G3"), perna_pp(590, 1, 1, fonte=42, cia="G3"),
        perna_pp(700, 1, 1, fonte=7, cia="AD")]
cand = f.idas_candidatas(idas, 3)
check("uma ida por fornecedor+companhia", [v["price"]["total"] for v in cand], [560.81, 577.86, 700])
check("teto de idas", len(f.idas_candidatas(idas, 2)), 2)
url_pp = f._url_pp(cfg_1x7, "THE", "2026-10-31", "2026-11-22")
check("link do Passagens Promo",
      url_pp.startswith("https://app.passagenspromo.com.br/loja/flight-list?departureDate=2026-10-31"),
      True)
print("      " + url_pp)

print("\nMelhores Destinos:")
PAGINA_MD = """
<article id="post-700" class="post"><h2>
  <a href="https://www.melhoresdestinos.com.br/promo-floripa-rio.html" title="x">Voos de Floripa para o Rio de Janeiro a partir de R$ 389 ida e volta</a>
</h2><span class="date">há 2 horas</span><p>Promo da Gol com taxas; também há opção por R$ 1.049 com bagagem.</p></article>
<article id="post-650" class="post"><h2>
  <a href="https://www.melhoresdestinos.com.br/voos-florianopolis-assuncao-gol.html">Gol terá voos de Florianópolis para o Paraguai</a>
</h2><span class="date">há 1 semana</span><p>Nova rota internacional &#8211; verão.</p></article>
<article id="post-640" class="post"><h2>
  <a href="https://www.melhoresdestinos.com.br/cataratas.html">Cataratas reabrem após cheia do rio</a>
</h2><span class="date">há 2 semanas</span><p>Foz do Iguaçu.</p></article>
"""
posts = f.extrair_posts(PAGINA_MD)
check("lê os três posts", [p.id for p in posts], [700, 650, 640])
check("título com entidades decodificadas", posts[1].resumo, "Nova rota internacional – verão.")
check("preços do título e do resumo", posts[0].precos, [389, 1049])
check("data relativa", posts[0].quando, "há 2 horas")
md_teste = {"termos_origem": ["florianópolis", "floripa"], "termos_destino": ["rio de janeiro", "rio"]}
rota = f.posts_da_rota(posts, md_teste)
check("só o post que cita as duas pontas", [p.id for p in rota], [700])
check("MD_PADRAO sem termos_destino configurado não casa nada (evita falso positivo)",
      f.posts_da_rota(posts, f.MD_PADRAO), [])
check("sem acento também casa",
      f._cita("Voos para Florianopolis e Galeao", ["florianópolis"]), True)
check("'rio' não casa dentro de outra palavra", f._cita("Hotel Riomar", ["rio"]), False)
check("linha do post mostra o menor preço", "a partir de R$ 389" in rota[0].linha(), True)
check("post novo é o que não foi visto", [p.id for p in m.posts_novos(posts, {650, 640})], [700])
check("nada novo quando tudo foi visto", m.posts_novos(posts, {700, 650, 640}), [])
aviso_md = m._sem_tags(m.mensagem_posts(cfg_1x7, rota))
check("aviso do blog tem link", "promo-floripa-rio.html" in aviso_md, True)
check("aviso do blog usa origem/destino da config", "Florianópolis ⇄ Piauí" in aviso_md, True)
msg_md = m._sem_tags(m.montar_mensagem(cfg_1x7, res, "alerta", 966, "teste", resumo=True, posts=rota))
check("resumo inclui a seção do blog", "━━ Melhores Destinos ━━" in msg_md, True)

print("\nNotificação por ntfy (título = primeira linha, sem tags):")
capturado = {}
ntfy_original = m.enviar_ntfy
m.enviar_ntfy = lambda titulo, texto, dry_run: capturado.update(titulo=titulo, texto=texto)
m.notificar("<b>🔥 Cabeçalho</b>\nlinha 2\nlinha 3", dry_run=True)
m.enviar_ntfy = ntfy_original
check("título do ntfy é a primeira linha sem tags", capturado["titulo"], "🔥 Cabeçalho")
check("corpo do ntfy é o resto da mensagem", capturado["texto"], "linha 2\nlinha 3")
check("NTFY_TOPIC ausente não derruba o envio (só pula)",
      m.enviar_ntfy("x", "y", dry_run=False), None)

# Regressao real: o workflow sempre declara NTFY_SERVER no ambiente, mesmo
# sem o secret cadastrado -- a variavel chega vazia, nao ausente. Isso
# derrubou uma execucao de producao com "unknown url type: '/'".
_ntfy_server_orig = os.environ.pop("NTFY_SERVER", None)
os.environ["NTFY_SERVER"] = ""
check("NTFY_SERVER vazio (secret nao cadastrado) cai no padrão",
      m._servidor_ntfy(), "https://ntfy.sh")
os.environ["NTFY_SERVER"] = "https://meu-ntfy.example.com/"
check("NTFY_SERVER customizado é respeitado (sem barra sobrando)",
      m._servidor_ntfy(), "https://meu-ntfy.example.com")
if _ntfy_server_orig is None:
    os.environ.pop("NTFY_SERVER", None)
else:
    os.environ["NTFY_SERVER"] = _ntfy_server_orig

print("\n" + ("TUDO OK" if not falhas else "FALHAS:\n  " + "\n  ".join(falhas)))
sys.exit(1 if falhas else 0)