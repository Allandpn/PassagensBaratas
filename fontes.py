# -*- coding: utf-8 -*-
"""
Fontes de preco do monitor.

Google Flights (via fast-flights)
    Busca com teto de preco crescente. O ranking "melhores voos" do Google
    esconde tarifas mais baratas; o primeiro teto que devolve resultado e o
    piso real da rota.

Vai de Promo
    API JSON publica do proprio site (mesma que o buscador deles usa no
    navegador). Permitida pelo robots.txt: so /redirect/, /safearea/ e as
    paginas de pagamento sao Disallow. Traz voo direto que o Google nao lista
    e separa o preco em tres numeros (ver "Bases de preco" abaixo).

Passagens Promo
    Loja white-label da OnerTravel (app.passagenspromo.com.br). O front chama
    serverless.api.onertravel.com: um POST abre a busca e devolve uma
    searchKey, e depois /search/outbound e /search/inbound listam ida e volta
    separadas, cada uma com o proprio preco. O ida e volta e a soma das duas.
    O robots.txt do dominio e "Disallow:" vazio -- tudo liberado.

Melhores Destinos
    Nao e buscador, e blog de promocao. Nao tem preco por data para consultar,
    entao entra como radar: le a listagem da categoria de promocoes e separa
    os posts que citam as duas pontas da rota. O /wp-json/ deles responde
    sempre a mesma lista congelada (ignora busca, data e categoria) e o /feed/
    e Disallow no robots.txt, por isso a leitura e do HTML da categoria.


Bases de preco
    O Vai de Promo devolve tres valores para a mesma tarifa, e a tela do site
    mostra o primeiro em destaque ("R$ 966,61 no PIX"):

        fare   -> tarifa, o que a tela chama de "Adultos (1x)"
        amount -> fare + taxa de embarque
        total  -> amount + taxa de servico (~10%), o "Preco total" da tela

    O Google Flights entrega um numero so, que ja inclui taxa de embarque e
    nao tem taxa de servico: equivale ao `amount`. Por isso guardamos os tres
    em toda oferta e a config escolhe qual deles dispara o alerta
    (`base_preco`) -- a mensagem sempre mostra os outros junto, para nao
    comparar tarifa de uma fonte com total da outra sem perceber.
"""
from __future__ import annotations

import html
import json
import random
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime

import primp
from fast_flights import FlightQuery, Passengers, create_query, get_flights

GOOGLE = "Google Flights"
VAIDEPROMO = "Vai de Promo"
PASSAGENSPROMO = "Passagens Promo"
MELHORESDESTINOS = "Melhores Destinos"
FONTES_DE_PRECO = (GOOGLE, VAIDEPROMO, PASSAGENSPROMO)

VDP_PROVIDERS = "https://site.aereo.vaidepromo.com.br/api/air/providers/{ori}/{des}/{data}/"
VDP_PAGINA = "https://www.vaidepromo.com.br/passagens-aereas/pesquisa/{token}/{ad}/0/0/Y/"
VDP_HEADERS = {
    "Referer": "https://www.vaidepromo.com.br/",
    "Accept": "application/json",
}
# Sem isso o primp usa um fingerprint TLS que o backend deles rejeita.
VDP_IMPERSONATE = "chrome_145"

BASES = ("tarifa", "com_taxas", "total")
BASE_PADRAO = "com_taxas"


def reais(valor: int | None) -> str:
    if valor is None:
        return "?"
    return f"{valor:,}".replace(",", ".")


@dataclass
class Oferta:
    preco: int            # base escolhida em cfg["base_preco"]; e o que alerta
    companhia: str
    rota: str
    partida: str
    chegada: str
    paradas: int
    destino: str
    fonte: str
    data_ida: str = ""
    data_volta: str = ""
    tarifa: int | None = None     # so o Vai de Promo separa a tarifa ("no PIX")
    com_taxas: int | None = None  # tarifa + taxa de embarque
    total: int | None = None      # tudo, ja com a taxa de servico

    def linha(self) -> str:
        paradas = "direto" if self.paradas == 0 else (
            "1 conexão" if self.paradas == 1 else f"{self.paradas} conexões"
        )
        partes = [
            f"<b>R$ {reais(self.preco)}</b> — {self.companhia} · {paradas}",
            f"   {self.rota} · sai {self.partida} · chega {self.chegada}",
        ]
        # So repete a decomposicao quando os numeros diferem de fato: no Google
        # os tres sao o mesmo valor e a linha extra seria ruido.
        valores = {v for v in (self.tarifa, self.com_taxas, self.total) if v is not None}
        if len(valores) > 1:
            partes.append(
                f"   <i>tarifa R$ {reais(self.tarifa)} · c/ taxas R$ {reais(self.com_taxas)}"
                f" · total R$ {reais(self.total)}</i>"
            )
        return "\n".join(partes)


@dataclass
class Resultado:
    fonte: str
    destino: str
    data_ida: str = ""
    data_volta: str = ""
    piso: int | None = None
    ofertas: list[Oferta] = field(default_factory=list)
    url: str = ""
    erro: str | None = None

    @property
    def rotulo(self) -> str:
        """Ex.: 'GIG 23/11' — identifica a combinacao no log e no historico."""
        if not self.data_volta:
            return self.destino
        return f"{self.destino} {datetime.fromisoformat(self.data_volta):%d/%m}"


class SemResultado(Exception):
    """A fonte respondeu ok, mas nao ha voo dentro do que foi pedido."""


def _tentar(fn, tentativas: int, log):
    """Repete com backoff. SemResultado passa direto, nao e falha."""
    ultimo = None
    for n in range(1, tentativas + 1):
        try:
            return fn()
        except SemResultado:
            raise
        except Exception as exc:
            ultimo = exc
            if n < tentativas:
                espera = 2 ** n + random.uniform(0, 1.5)
                log(f"    tentativa {n} falhou ({type(exc).__name__}), repetindo em {espera:.1f}s")
                time.sleep(espera)
    raise RuntimeError(f"{type(ultimo).__name__}: {ultimo}")


def escolher_base(cfg: dict, tarifa: int, com_taxas: int, total: int) -> int:
    base = cfg.get("base_preco", BASE_PADRAO)
    return {"tarifa": tarifa, "com_taxas": com_taxas, "total": total}.get(base, com_taxas)


# --------------------------------------------------------------------------- #
# Google Flights
# --------------------------------------------------------------------------- #

def _query_google(cfg: dict, destino: str, ida: str, volta: str, teto: int | None):
    return create_query(
        flights=[
            FlightQuery(date=ida, from_airport=cfg["origem"], to_airport=destino),
            FlightQuery(date=volta, from_airport=destino, to_airport=cfg["origem"]),
        ],
        seat=cfg["classe"],
        trip="round-trip",
        passengers=Passengers(adults=cfg["adultos"]),
        language=cfg["idioma"],
        currency=cfg["moeda"],
        max_price=teto,
    )


def _oferta_google(voo, destino: str, ida: str, volta: str) -> Oferta:
    pernas = voo.flights
    d, a = pernas[0].departure, pernas[-1].arrival
    preco = int(voo.price)
    return Oferta(
        # O Google nao separa tarifa de taxa de embarque: o numero dele ja e o
        # "com taxas". Repetimos nos tres campos para o alerta funcionar em
        # qualquer base_preco, e linha() omite a decomposicao por serem iguais.
        preco=preco,
        tarifa=preco,
        com_taxas=preco,
        total=preco,
        companhia="/".join(voo.airlines) or voo.type,
        rota="-".join([pernas[0].from_airport.code] + [p.to_airport.code for p in pernas]),
        partida=f"{d.date[2]:02d}/{d.date[1]:02d} {d.time[0]:02d}:{d.time[1]:02d}",
        chegada=f"{a.date[2]:02d}/{a.date[1]:02d} {a.time[0]:02d}:{a.time[1]:02d}",
        paradas=len(pernas) - 1,
        destino=destino,
        fonte=GOOGLE,
        data_ida=ida,
        data_volta=volta,
    )


def buscar_google(cfg: dict, destino: str, ida: str, volta: str,
                  proxy: str | None, log) -> Resultado:
    res = Resultado(
        fonte=GOOGLE, destino=destino, data_ida=ida, data_volta=volta,
        url=_query_google(cfg, destino, ida, volta, None).url(),
    )
    pausa = cfg["pausa_entre_buscas_seg"]

    for teto in list(cfg["degraus_preco"]) + [None]:
        rotulo = f"ate R$ {teto}" if teto else "sem teto"
        query = _query_google(cfg, destino, ida, volta, teto)

        def chamar():
            try:
                return get_flights(query, proxy=proxy) if proxy else get_flights(query)
            except TypeError:
                # A lib estoura TypeError quando o payload de voos vem vazio.
                # E "nenhum voo dentro do teto", nao falha de rede.
                raise SemResultado

        try:
            voos = _tentar(chamar, cfg["tentativas"], log)
        except SemResultado:
            log(f"  [Google] {res.rotulo} {rotulo}: nada")
            time.sleep(pausa)
            continue
        except RuntimeError as exc:
            log(f"  [Google] {res.rotulo} {rotulo}: ERRO {exc}")
            res.erro = str(exc)
            time.sleep(pausa)
            continue

        res.ofertas = sorted((_oferta_google(v, destino, ida, volta) for v in voos),
                             key=lambda o: o.preco)
        res.piso = res.ofertas[0].preco
        res.erro = None
        log(f"  [Google] {res.rotulo} {rotulo}: {len(voos)} ofertas, piso R$ {res.piso}")
        return res

    return res


# --------------------------------------------------------------------------- #
# Vai de Promo
# --------------------------------------------------------------------------- #

def _aammdd(data_iso: str) -> str:
    return datetime.fromisoformat(data_iso).strftime("%y%m%d")


def _token_vdp(cfg: dict, destino: str, ida: str, volta: str) -> str:
    """Ida e volta usam hifen: FLNGIG261031-GIGFLN261122 (descoberto testando a API)."""
    ori = cfg["origem"]
    return f"{ori}{destino}{_aammdd(ida)}-{destino}{ori}{_aammdd(volta)}"


def _oferta_vdp(cfg: dict, dados: dict, rec: dict, destino: str,
                ida: str, volta: str) -> Oferta | None:
    preco = dados["prices"][rec["prices"][0]]
    trechos = []
    for perna, opcoes in enumerate(rec["itineraries"]):
        if not opcoes:
            continue
        try:
            trechos.append(dados["itineraries"][perna][opcoes[0]])
        except (IndexError, KeyError):
            return None
    if not trechos:
        return None

    jornada = trechos[0]["journeys"][0]
    segmentos = jornada.get("segments") or []
    if not segmentos:
        return None

    def hora(carimbo: str) -> str:
        try:
            dt = datetime.fromisoformat(carimbo)
            return f"{dt.day:02d}/{dt.month:02d} {dt.hour:02d}:{dt.minute:02d}"
        except ValueError:
            return "?"

    codigos = {s.get("marketing_carrier_code") for s in segmentos if s.get("marketing_carrier_code")}
    nomes = {c["code"]: c["name"] for c in dados.get("carriers", [])}
    rota = "-".join(
        [segmentos[0]["departure"]["location"]] + [s["arrival"]["location"] for s in segmentos]
    )
    # fare e o numero grande da tela ("no PIX"), amount soma a taxa de embarque
    # e total ainda soma a taxa de servico. Ver "Bases de preco" no topo.
    total = round(preco.get("total") or 0)
    com_taxas = round(preco.get("amount") or total)
    tarifa = round(preco.get("fare") or com_taxas)
    return Oferta(
        preco=escolher_base(cfg, tarifa, com_taxas, total),
        tarifa=tarifa,
        com_taxas=com_taxas,
        total=total,
        companhia="/".join(sorted(nomes.get(c, c) for c in codigos)) or "?",
        rota=rota,
        partida=hora(segmentos[0]["departure"]["date"]),
        chegada=hora(segmentos[-1]["arrival"]["date"]),
        paradas=int(jornada.get("total_stops") or 0),
        destino=destino,
        fonte=VAIDEPROMO,
        data_ida=ida,
        data_volta=volta,
    )


def _cliente_vdp(cfg: dict):
    return primp.Client(impersonate=VDP_IMPERSONATE, timeout=cfg.get("timeout_vdp_seg", 120))


def _json_vdp(cliente, url: str) -> dict:
    r = cliente.get(url, headers=VDP_HEADERS)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return json.loads(r.text)


def listar_providers(cfg: dict, destino: str, ida: str, log, cliente=None) -> list[dict]:
    """Companhias que atendem a rota. Nao depende da data de volta, entao o
    monitor busca uma vez por destino e reaproveita em toda a janela."""
    cliente = cliente or _cliente_vdp(cfg)
    url = VDP_PROVIDERS.format(ori=cfg["origem"], des=destino, data=_aammdd(ida))
    return _tentar(lambda: _json_vdp(cliente, url), cfg["tentativas"], log).get("providers", [])


def urls_de_busca(cfg: dict, providers: list[dict], token: str) -> dict[str, str]:
    """A lista vem com repeticoes (I8 tres vezes, WB e TS duas) que diferem so
    no mileage_carrier e produzem exatamente a mesma URL. Sem o dedup a rodada
    dispara 10 requisicoes onde existem 6 buscas distintas."""
    urls: dict[str, str] = {}
    for prov in providers:
        codigo = prov.get("code")
        base = prov.get("endpoint")
        if not codigo or not base:
            continue
        urls.setdefault(codigo, f"{base}/api/search/{token}/{cfg['adultos']}/0/0/Y/{codigo}")
    return urls


def buscar_vaidepromo(cfg: dict, destino: str, ida: str, volta: str, log,
                      providers: list[dict] | None = None) -> Resultado:
    token = _token_vdp(cfg, destino, ida, volta)
    res = Resultado(
        fonte=VAIDEPROMO,
        destino=destino,
        data_ida=ida,
        data_volta=volta,
        url=VDP_PAGINA.format(token=token, ad=cfg["adultos"]),
    )
    cliente = _cliente_vdp(cfg)
    pausa = cfg["pausa_entre_buscas_seg"]

    if providers is None:
        try:
            providers = listar_providers(cfg, destino, ida, log, cliente)
        except RuntimeError as exc:
            log(f"  [VaiDePromo] {res.rotulo}: ERRO ao listar companhias — {exc}")
            res.erro = str(exc)
            return res

    todas: list[Oferta] = []
    falhas: list[str] = []
    for codigo, url in urls_de_busca(cfg, providers, token).items():
        try:
            dados = _tentar(lambda u=url: _json_vdp(cliente, u), cfg["tentativas"], log)
        except RuntimeError as exc:
            log(f"  [VaiDePromo] {res.rotulo} {codigo}: ERRO {exc}")
            falhas.append(codigo)
            time.sleep(pausa)
            continue

        if dados.get("errors"):
            log(f"  [VaiDePromo] {res.rotulo} {codigo}: sem tarifa")
            time.sleep(pausa)
            continue

        do_provider = [
            o for o in (_oferta_vdp(cfg, dados, rec, destino, ida, volta)
                        for rec in dados.get("recommendations", []))
            if o is not None
        ]
        if do_provider:
            log(f"  [VaiDePromo] {res.rotulo} {codigo}: {len(do_provider)} tarifas, "
                f"piso R$ {min(o.preco for o in do_provider)}")
            todas.extend(do_provider)
        time.sleep(pausa)

    if todas:
        res.ofertas = sorted(todas, key=lambda o: o.preco)
        res.piso = res.ofertas[0].preco
    elif falhas:
        res.erro = "falha nas companhias: " + ", ".join(falhas)
    return res


# --------------------------------------------------------------------------- #
# Passagens Promo (OnerTravel)
# --------------------------------------------------------------------------- #

PP_API = "https://serverless.api.onertravel.com/api/flight/v1"
PP_PAGINA = "https://app.passagenspromo.com.br/loja/flight-list"
# Os mesmos cabecalhos que o interceptor do front poe em toda chamada. Sem o
# ApplicationName a API responde 200 com corpo vazio em vez de erro.
PP_HEADERS = {
    "Origin": "https://app.passagenspromo.com.br",
    "Referer": "https://app.passagenspromo.com.br/",
    "Accept": "application/json",
    "Authorization": "Bearer ",
    "Language": "4",
    "Currencie": "1",
    "Currency": "1",
    "Platform": "WEBAPP",
    "InstitutionId": "41",
    "AgentId": "86185",
    "ApplicationAccessType": "1",
    "ApplicationName": "PASSAGENSPROMO",
    "X-Location-href": PP_PAGINA,
}
PP_MENOR_PRECO = 0      # ordinationEnum: LOWERVALUE
PP_INTERVALO_SEG = 6    # entre as leituras da lista enquanto a busca enche


def _url_pp(cfg: dict, destino: str, ida: str, volta: str) -> str:
    return PP_PAGINA + "?" + urllib.parse.urlencode({
        "departureDate": ida, "returnDate": volta, "isRoundTrip": "true",
        "adultsCount": cfg["adultos"], "childCount": 0, "infantCount": 0, "teenagerCount": 0,
        "departureIata": cfg["origem"], "arrivalIata": destino,
        "isDepartureIataCity": "false", "isArrivalIataCity": "true", "source": "f",
    })


def _post_pp(cliente, caminho: str, corpo: dict, extra: dict | None = None) -> dict:
    r = cliente.post(PP_API + caminho, headers={**PP_HEADERS, **(extra or {})}, json=corpo)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    if not r.text.strip():
        raise RuntimeError("resposta vazia (cabeçalhos recusados?)")
    return json.loads(r.text)


def _pagina_pp(chave: str, voo_ida: str | None = None) -> dict:
    corpo = {"searchKey": chave, "page": 1, "pageSize": 10,
             "filter": {"maxStopsEnum": 0}, "ordinationEnum": PP_MENOR_PRECO}
    if voo_ida:
        corpo["flightKey"] = voo_ida
    return corpo


def _idas_pp(cliente, cfg: dict, chave: str) -> list[dict]:
    """O site espera um websocket avisar que as companhias responderam. Aqui
    basta reler a lista ate a contagem parar de mudar entre duas leituras."""
    limite = time.monotonic() + cfg.get("timeout_pp_seg", 90)
    anterior = None
    while True:
        time.sleep(PP_INTERVALO_SEG)
        dados = _post_pp(cliente, "/search/outbound", _pagina_pp(chave))
        total = dados.get("totalFlightsCount") or 0
        if (total and total == anterior) or time.monotonic() > limite:
            return dados.get("flights") or []
        anterior = total


def _hora_pp(ponto: dict) -> str:
    d, t = ponto.get("date") or {}, ponto.get("time") or {}
    try:
        return f"{d['day']:02d}/{d['month']:02d} {t['hour']:02d}:{t['minute']:02d}"
    except (KeyError, TypeError):
        return "?"


def _oferta_pp(cfg: dict, ida: dict, volta: dict, destino: str,
               data_ida: str, data_volta: str) -> Oferta:
    """Ida e volta vem com preco proprio; o ida e volta e a soma. Em cada perna
    `price` e a tarifa, `tax` a taxa de embarque e `total` = price + tax. A
    taxa de servico (`serviceTax`) ja vem embutida no `price`, entao aqui
    com_taxas e total saem iguais -- ao contrario do Vai de Promo, que cobra a
    taxa de servico por fora."""
    pi, pv = ida["price"], volta["price"]
    ji = ida["journey"]
    segmentos = ji.get("segments") or []
    rota = "-".join([ji["departure"]["iata"]] + [s["destination"]["iata"] for s in segmentos]) \
        if segmentos else f"{ji['departure']['iata']}-{ji['destination']['iata']}"
    companhias = {
        (j.get("marketingAirline") or {}).get("name", "").strip().title()
        for j in (ji, volta["journey"])
    } - {""}
    tarifa = round(pi["price"] + pv["price"])
    com_taxas = round(pi["price"] + pi["tax"] + pv["price"] + pv["tax"])
    total = round(pi["total"] + pv["total"])
    return Oferta(
        preco=escolher_base(cfg, tarifa, com_taxas, total),
        tarifa=tarifa,
        com_taxas=com_taxas,
        total=total,
        companhia="/".join(sorted(companhias)) or "?",
        rota=rota,
        partida=_hora_pp(ji["departure"]),
        chegada=_hora_pp(ji["destination"]),
        paradas=int(ji.get("numberOfStops") or 0),
        destino=ji["destination"].get("iata") or destino,
        fonte=PASSAGENSPROMO,
        data_ida=data_ida,
        data_volta=data_volta,
    )


def idas_candidatas(voos: list[dict], maximo: int) -> list[dict]:
    """A volta so pode ser do mesmo fornecedor e companhia da ida escolhida, e
    a ida mais barata nem sempre tem a volta mais barata (LATAM R$ 561 de ida
    pedia R$ 951 de volta; Gol R$ 578 de ida tinha volta de R$ 571). Por isso
    guarda a ida mais barata de cada (fornecedor, companhia)."""
    vistos, saida = set(), []
    for voo in sorted(voos, key=lambda v: v["price"]["total"]):
        par = (voo.get("source"), (voo["journey"].get("marketingAirline") or {}).get("iata"))
        if par in vistos:
            continue
        vistos.add(par)
        saida.append(voo)
        if len(saida) >= maximo:
            break
    return saida


def buscar_passagenspromo(cfg: dict, destino: str, ida: str, volta: str, log) -> Resultado:
    res = Resultado(fonte=PASSAGENSPROMO, destino=destino, data_ida=ida, data_volta=volta,
                    url=_url_pp(cfg, destino, ida, volta))
    cliente = primp.Client(impersonate=VDP_IMPERSONATE, timeout=cfg.get("timeout_vdp_seg", 120))
    pausa = cfg["pausa_entre_buscas_seg"]
    busca = {
        "departureDate": ida, "returnDate": volta,
        "departureStation": cfg["origem"], "arrivalStation": destino,
        "paxAdtCount": cfg["adultos"], "paxChdCount": 0, "paxInfCount": 0,
    }

    try:
        chave = _tentar(lambda: _post_pp(cliente, "/search", busca, {"isPackage": "false"}),
                        cfg["tentativas"], log).get("searchKey")
        if not chave:
            raise RuntimeError("busca sem searchKey")
        idas = _tentar(lambda: _idas_pp(cliente, cfg, chave), cfg["tentativas"], log)
    except RuntimeError as exc:
        log(f"  [PassagensPromo] {res.rotulo}: ERRO {exc}")
        res.erro = str(exc)
        return res

    if not idas:
        log(f"  [PassagensPromo] {res.rotulo}: sem voo de ida")
        return res

    ofertas: list[Oferta] = []
    for voo_ida in idas_candidatas(idas, cfg.get("max_idas_passagenspromo", 3)):
        time.sleep(pausa)
        try:
            voltas = _tentar(
                lambda k=voo_ida["key"]: _post_pp(cliente, "/search/inbound", _pagina_pp(chave, k)),
                cfg["tentativas"], log,
            ).get("flights") or []
        except RuntimeError as exc:
            log(f"  [PassagensPromo] {res.rotulo} volta: ERRO {exc}")
            res.erro = str(exc)
            continue
        if voltas:
            melhor_volta = min(voltas, key=lambda v: v["price"]["total"])
            ofertas.append(_oferta_pp(cfg, voo_ida, melhor_volta, destino, ida, volta))

    if ofertas:
        res.ofertas = sorted(ofertas, key=lambda o: o.preco)
        res.piso = res.ofertas[0].preco
        res.erro = None
        log(f"  [PassagensPromo] {res.rotulo}: {len(idas)} idas, {len(ofertas)} combinações, "
            f"piso R$ {res.piso}")
    return res


# --------------------------------------------------------------------------- #
# Melhores Destinos (radar de posts)
# --------------------------------------------------------------------------- #

MD_CATEGORIA = "https://www.melhoresdestinos.com.br/category/{slug}"
MD_ARTIGO = re.compile(r'<article id="post-(\d+)"(.*?)</article>', re.S)
MD_LINK = re.compile(r'<h2>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
MD_DATA = re.compile(r'<span class="date">(.*?)</span>', re.S)
MD_RESUMO = re.compile(r"<p>(.*?)</p>", re.S)
REAIS_NO_TEXTO = re.compile(r"R\$\s?(\d{1,3}(?:\.\d{3})+|\d+)")

# termos_destino fica vazio de proposito: a rota muda por config.json, e um
# post so entra no radar quando as duas pontas (origem e destino) sao citadas.
MD_PADRAO = {
    "categorias": ["promocoes-passagens-aereas"],
    "paginas": 1,
    "termos_origem": ["florianópolis", "floripa", "FLN"],
    "termos_destino": [],
}


@dataclass
class Post:
    id: int
    titulo: str
    link: str
    resumo: str
    quando: str                 # o site so da data relativa ("há 2 dias")
    precos: list[int] = field(default_factory=list)

    def linha(self) -> str:
        preco = f" · a partir de R$ {reais(min(self.precos))}" if self.precos else ""
        return f'• <a href="{self.link}">{html.escape(self.titulo)}</a>{preco} <i>({self.quando})</i>'


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower())
                   if unicodedata.category(c) != "Mn")


def _cita(texto: str, termos: list[str]) -> bool:
    alvo = _sem_acento(texto)
    return any(re.search(rf"\b{re.escape(_sem_acento(t))}\b", alvo) for t in termos)


def _texto(fragmento: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragmento)).strip()


def extrair_posts(pagina: str) -> list[Post]:
    posts = []
    for pid, corpo in MD_ARTIGO.findall(pagina):
        link = MD_LINK.search(corpo)
        if not link:
            continue
        data, resumo = MD_DATA.search(corpo), MD_RESUMO.search(corpo)
        titulo = _texto(link.group(2))
        resumo_txt = _texto(resumo.group(1)) if resumo else ""
        precos = [int(p.replace(".", "")) for p in REAIS_NO_TEXTO.findall(f"{titulo} {resumo_txt}")]
        posts.append(Post(id=int(pid), titulo=titulo, link=link.group(1), resumo=resumo_txt,
                          quando=_texto(data.group(1)) if data else "", precos=precos))
    return posts


def posts_da_rota(posts: list[Post], md: dict) -> list[Post]:
    """So interessa post que cita as duas pontas: 'Floripa' sozinho traz voo
    para outro destino qualquer, e o nome do destino sozinho pode aparecer em
    contexto que nada tem a ver com a rota (ex.: "cheia do rio" em Foz)."""
    if not md.get("termos_destino"):
        return []
    return [p for p in posts
            if _cita(f"{p.titulo} {p.resumo}", md["termos_origem"])
            and _cita(f"{p.titulo} {p.resumo}", md["termos_destino"])]


def buscar_melhoresdestinos(cfg: dict, log) -> list[Post]:
    md = {**MD_PADRAO, **cfg.get("melhoresdestinos", {})}
    cliente = primp.Client(impersonate=VDP_IMPERSONATE, timeout=60)
    todos: dict[int, Post] = {}
    for slug in md["categorias"]:
        for n in range(1, md["paginas"] + 1):
            url = MD_CATEGORIA.format(slug=slug) + (f"/page/{n}" if n > 1 else "")

            def ler(u=url):
                r = cliente.get(u)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                return r.text

            posts = extrair_posts(_tentar(ler, cfg["tentativas"], log))
            if not posts:
                raise RuntimeError(f"nenhum post reconhecido em {url} (layout mudou?)")
            for p in posts:
                todos.setdefault(p.id, p)
    achados = sorted(posts_da_rota(list(todos.values()), md), key=lambda p: -p.id)
    log(f"  [MelhoresDestinos] {len(todos)} posts lidos, {len(achados)} sobre a rota")
    return achados
