# Monitor de passagens — Florianópolis ⇄ Piauí

Roda de madrugada na nuvem (GitHub Actions), varre o Google Flights, o Vai de
Promo e o Passagens Promo numa **janela de datas**, e manda um alerta no
Telegram quando a ida e volta cai para a faixa de preço que você quer. De
quebra, fica de olho no blog Melhores Destinos e avisa quando sai post de
promoção citando as duas pontas da rota.

| Trecho | Datas | Alvo |
|---|---|---|
| FLN ⇄ THE + PHB (Piauí), 1 pessoa, econômica | ida 20/11 ou 23/11/2026 · volta 04/12 ou 07/12/2026 | 🔥🔥 < R$ 1.400 · 🔥 R$ 1.400–1.600 · ⚠️ R$ 1.600–1.700 |

Os exemplos abaixo (datas, valores, trecho FLN-RIO) são das rodadas em que o
recurso de janela de datas e bases de preço foi criado — mantidos porque
ilustram o mecanismo, não o destino monitorado hoje.

## Janela de datas

`data_ida` e `data_volta` aceitam três formatos. Cada combinação (ida × volta)
vira uma busca em cada fonte, então a janela é o que mais pesa no tempo da
rodada.

```json
"data_ida":   "2026-10-31",                              // uma data
"data_volta": { "de": "2026-11-22", "ate": "2026-11-28" }, // intervalo, dia a dia
"data_volta": ["2026-11-22", "2026-11-28"]                 // só essas duas
```

Combinações com volta anterior à ida são descartadas sozinhas, e
`max_combinacoes_datas` (padrão 12) é o freio: se a janela pedir mais que isso,
o monitor corta e avisa no log em vez de estourar o runner.

Vale a pena: na primeira execução com a janela 22–28/11, a volta em **23/11
saiu R$ 288 mais barata** que a 22/11 que estava fixa antes. A mensagem traz o
piso de cada data de volta justamente para responder "vale esticar a viagem?":

```
━━ Piso por data de volta ━━
22/11 · R$ 1.255
23/11 · R$ 967 ⭐
```

## Bases de preço — qual número é o alerta

O Vai de Promo devolve **três valores para a mesma tarifa**, e a tela do site
mostra o primeiro em destaque ("R$ 966,61 no PIX"):

| Campo da API | Nome aqui | O que é na tela do site |
|---|---|---|
| `fare` | `tarifa` | "Adultos (1x)" — o número grande, "no PIX" |
| `amount` | `com_taxas` | tarifa + taxa de embarque |
| `total` | `total` | + taxa de serviço (~10%) — o "Preço total" |

O Google Flights entrega **um número só**, que já inclui taxa de embarque e não
tem taxa de serviço: equivale ao `com_taxas`.

Confundir os três é o erro fácil — comparar a tarifa de uma fonte com o total
da outra faz o Vai de Promo parecer 30% mais barato do que é. Por isso:

- toda oferta carrega os três valores, e a mensagem os mostra lado a lado:

  ```
  R$ 967 — LATAM · 1 conexão
     FLN-GRU-SDU · sai 31/10 05:25 · chega 31/10 15:55
     tarifa R$ 967 · c/ taxas R$ 1.148 · total R$ 1.245
  ```

- `"base_preco"` no config escolhe qual deles dispara o alerta, e a mensagem
  diz explicitamente qual está valendo:

  | Valor | Alerta compara | Quando usar |
  |---|---|---|
  | `"tarifa"` | o número "no PIX" | **padrão** — é o que você vê no site e o que seus alvos de 800/900/1000 refletem |
  | `"com_taxas"` | tarifa + embarque | comparação justa entre as duas fontes |
  | `"total"` | tudo, com taxa de serviço | o que sai do bolso |

  Com `"tarifa"`, lembre que o Google não separa taxa de embarque: o número
  dele entra na comparação já com taxas, então o Google aparece penalizado.

## Fontes

### Google Flights — busca por teto de preço

A página do Google Flights mostra por padrão os "melhores voos" — um ranking que
mistura preço, duração e conexões, e que **omite tarifas mais baratas**. Em vez de
ler esse ranking, o monitor faz a busca com **teto de preço crescente**:

```
busca com max_price=800   → vazio
busca com max_price=900   → vazio
busca com max_price=1000  → vazio
busca com max_price=1200  → 5 ofertas, a partir de R$ 1.208   ← piso real
```

O primeiro teto que devolve resultado é o piso verdadeiro da rota. Em teste, o
ranking padrão mostrava R$ 1.306 enquanto existia voo por R$ 1.208.

### Vai de Promo — API JSON pública

Mesma API que o buscador deles usa no navegador, em três endpoints:

```
GET site.aereo.vaidepromo.com.br/api/air/providers/FLN/GIG/261031/
    → [{"code":"AD",...},{"code":"G3",...},{"code":"LA",...}]

GET flights.vaidepromo.com.br/api/search/FLNGIG261031-GIGFLN261123/1/0/0/Y/G3
    → {"prices":[...], "recommendations":[...], "itineraries":[[ida],[volta]]}
```

O token de ida e volta usa **hífen** entre os trechos (`FLNGIG261031-GIGFLN261123`);
sem ele a API devolve HTTP 500. Permitido pelo `robots.txt` deles — só
`/redirect/`, `/safearea/` e as páginas de pagamento são `Disallow`.

Duas economias importam na janela de datas:

- a **lista de companhias** depende só de origem/destino/ida, não da volta —
  é buscada uma vez por destino e reaproveitada nas 7 datas;
- a lista vem com **repetições** (`I8` três vezes, `WB` e `TS` duas) que diferem
  apenas no `mileage_carrier` e geram a mesma URL de busca. Deduplicando,
  10 entradas viram 6 requisições.

Traz voo direto que o Google não lista (ex.: Gol 1963 FLN→GIG sem escala) e é
onde aparecem as tarifas mais baixas quando há promo.

### Passagens Promo — API da OnerTravel

A loja (`app.passagenspromo.com.br`) é um white-label da OnerTravel. O front
fala com `serverless.api.onertravel.com` em três passos:

```
POST /api/flight/v1/search            {"departureStation":"FLN","arrivalStation":"THE",...}
    → {"searchKey":"45e3e651-..."}
POST /api/flight/v1/search/outbound   {"searchKey":...,"ordinationEnum":0}   ← idas, mais barata primeiro
POST /api/flight/v1/search/inbound    {"searchKey":...,"flightKey":<ida>}    ← voltas daquela ida
```

- Aceita código de cidade (ex.: `RIO` traz Galeão e Santos Dumont numa busca
  só); para aeroportos sem agregador de cidade, usa o código do aeroporto.
- O site espera um websocket avisar que as companhias responderam; o monitor
  apenas relê a lista de idas até a contagem parar de mudar.
- **Ida e volta têm preço separado** e o ida e volta é a soma. A volta mais
  barata depende da ida escolhida (LATAM R$ 561 de ida pedia R$ 951 de volta;
  Gol R$ 578 de ida tinha volta de R$ 571), então o monitor testa a ida mais
  barata de cada companhia/fornecedor (`max_idas_passagenspromo`, padrão 3).
- A taxa de serviço vem embutida na tarifa: aqui `com_taxas` e `total` saem
  iguais.
- **HTTP 200 com corpo vazio não é erro** no `/search/inbound`: é a API
  dizendo "essa ida não tem volta desse fornecedor nessa data" — confirmado
  ao vivo comparando a mesma sessão/cabeçalhos numa rota que funciona (Rio) e
  numa que não tem volta (Parnaíba: todas as 7 idas testadas vieram vazias no
  `/search/inbound`, enquanto o `/search` e o `/search/outbound` funcionaram
  normalmente). O monitor loga como "sem volta pra essa ida" e segue pra
  próxima, sem contar como falha.
- Assim como no Vai de Promo, é preciso impersonar o TLS de um navegador
  (`primp`): sem isso a API responde 200 vazio em qualquer chamada.

`robots.txt` do domínio: `Disallow:` vazio, tudo liberado.

### Melhores Destinos — radar de posts

Não é buscador, é blog: não existe "preço de 20/11 a 04/12" para consultar.
Entra como radar. A cada rodada o monitor lê a listagem da categoria
*Promoções de passagens aéreas* e separa os posts que citam **as duas pontas**
da rota (só o nome da origem, ou só o do destino, pode aparecer em post sobre
outra rota qualquer). Cada post novo gera um Telegram próprio, uma vez só — os
ids já avisados ficam em `state/melhoresdestinos_vistos.json`:

```
📰 Melhores Destinos: post novo sobre Florianópolis ⇄ Piauí
• Voos de Floripa para o Rio a partir de R$ 389 · a partir de R$ 389 (há 2 horas)
```

O resumo do meio-dia também lista os posts da rota que estão na página. Nada
disso mexe no piso de preço nem no anti-spam dos alertas. Por padrão o
`termos_destino` vem vazio (ver `fontes.MD_PADRAO`) — sem configurar os termos
do destino atual em `config.json`, o radar não casa post nenhum, de propósito,
para não gerar falso positivo.

O `/wp-json/` deles responde sempre a mesma lista congelada (ignora busca,
data e categoria) e o `/feed/` é `Disallow` no `robots.txt`, por isso a leitura
é do HTML da categoria, que é liberado. Assim como as outras duas APIs não
oficiais, a leitura só funciona impersonando o TLS de um navegador — com
`curl` puro o site devolve 403 (WAF de fingerprint, não de IP).

### Skyscanner — fora, de propósito

Não é dificuldade técnica. O `robots.txt` deles, no bloco `User-agent: *`, põe:

```
Disallow: /transporte/*      ← a busca de voos
Disallow: /dataservices/*    ← API interna de preço
Disallow: /skippy_api/*      ← API interna de preço
```

Eles pedem explicitamente para bots não automatizarem a busca. Somado a isso,
o site é protegido por Imperva/Incapsula com desafio JS (`reese84`), que de um
IP de datacenter como o do GitHub Actions só passaria com serviço pago de
bypass. Ficou de fora.

## Instalação

**1. Bot do Telegram** (2 minutos)

- No Telegram, fale com [@BotFather](https://t.me/BotFather) → `/newbot` → copie o token
- Abra a conversa do bot que você acabou de criar e mande `oi`
- Rode:

```bash
pip install -r requirements.txt
python setup_telegram.py SEU_TOKEN_AQUI
```

Ele imprime o `TELEGRAM_CHAT_ID` e manda uma mensagem de teste.

**1b. Push no celular via ntfy** (opcional, 1 minuto)

- Instale o app [ntfy](https://ntfy.sh/app) (Android/iOS) e inscreva-se num
  tópico (o nome do tópico funciona como senha — escolha algo difícil de
  adivinhar, tipo `passagens-baratas-XXXXXXX`).
- Guarde o nome do tópico como `NTFY_TOPIC`. Se usar servidor próprio em vez
  do público `ntfy.sh`, guarde a URL como `NTFY_SERVER`.
- Sem `NTFY_TOPIC` configurado, o monitor simplesmente não manda nada por
  esse canal — o Telegram continua funcionando normalmente.

**2. Subir para o GitHub**

```bash
git add -A
git commit -m "monitor de voos FLN <-> Rio"
gh repo create voos-fln-rio --private --source=. --push
# ou crie o repo pelo site e: git remote add origin ... && git push -u origin main
```

**3. Cadastrar os segredos**

No repositório: **Settings → Secrets and variables → Actions → New repository secret**

| Nome | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token do BotFather |
| `TELEGRAM_CHAT_ID` | número que o `setup_telegram.py` imprimiu |
| `NTFY_TOPIC` | *(opcional)* tópico do ntfy pra push no celular |
| `NTFY_SERVER` | *(opcional)* só se usar servidor próprio em vez do `ntfy.sh` |
| `PROXY_URL` | *(opcional)* proxy, caso o Google bloqueie o IP do runner |

**4. Testar**

Aba **Actions → Monitor de voos FLN <-> Rio → Run workflow**. Deixe
"Mandar mensagem mesmo sem promoção" marcado — deve chegar um Telegram em ~10 min.

## Horários

Cinco execuções por dia, em BRT:

| Horário | O que faz |
|---|---|
| 01:00, 02:30, 04:00, 05:30 | varredura da madrugada — só avisa se estiver abaixo de R$ 1.000 |
| 12:00 | resumo diário com o piso do dia e a tendência |

O cron do GitHub pode atrasar alguns minutos em horário de pico. É esperado.

## Quando ele avisa

| Situação | Manda Telegram? |
|---|---|
| Piso < R$ 1.000 pela primeira vez | ✅ |
| Piso caiu abaixo do último valor avisado | ✅ |
| Mesmo preço, já avisado há menos de 12 h | ❌ (anti-spam) |
| Mesmo preço, avisado há mais de 12 h | ✅ lembrete |
| Piso ≥ R$ 1.000 | ❌ (só entra no resumo do meio-dia) |
| Nenhuma fonte respondeu | ⚠️ avisa que o monitor quebrou |

O piso é o menor preço de **toda a janela** — se a promoção está só na volta do
dia 26, o alerta dispara mesmo assim e a mensagem diz qual data é.

## Ajustes

Tudo em `config.json`:

```json
"data_volta":            { "de": "2026-11-22", "ate": "2026-11-28" },
"max_combinacoes_datas": 12,
"base_preco":            "tarifa",
"alvos":                 { "jackpot": 800, "alerta": 900, "aviso": 1000 },
"degraus_preco":         [800, 900, 1000, 1200],
"antispam_horas":        12
```

- **Esticar a janela**: mude o `"ate"`. Cada dia a mais é +1 busca no Google
  por destino e +1 no Vai de Promo — conte ~50 s por dia extra.
- **Janela na ida também**: `"data_ida": {"de": "2026-10-30", "ate": "2026-11-01"}`.
  Cuidado: multiplica, não soma — 3 idas × 7 voltas = 21 combinações, e o teto
  de 12 corta o excesso.
- **Alertar por outro número**: `"base_preco"` entre `"tarifa"`, `"com_taxas"`
  e `"total"` (ver *Bases de preço*).
- Quer ser avisado até R$ 1.200? Troque `"aviso": 1000` por `1200` e inclua
  `1200` nos degraus.
- Menos consultas por rodada: deixe `"destinos": ["RIO"]` (o código `RIO` já
  cobre Galeão e Santos Dumont; GIG e SDU separados só melhoram a cobertura).
- `"destinos"` são as consultas no Google; `"destinos_vaidepromo"` as do Vai de
  Promo, que não aceita código de cidade — só aeroporto (`GIG`, `SDU`).
  Deixe `"destinos_vaidepromo": []` para desligar essa fonte.
- `"destinos_passagenspromo"` aceita cidade (ex.: `RIO`) ou aeroporto; `[]`
  desliga a fonte. `"max_idas_passagenspromo"` (padrão 3) é quantas idas
  diferentes o monitor testa por companhia/fornecedor antes de buscar a
  volta de cada uma — subir esse número custa mais requisições e tempo.
- `"melhoresdestinos"`: `"ativo": false` desliga o radar do blog;
  `"termos_origem"` e `"termos_destino"` são as palavras que o post precisa
  citar (sem diferença de acento ou maiúscula) — o padrão vem com
  `termos_destino` vazio de propósito, então **é preciso configurar os termos
  do destino atual** ou o radar nunca acha nada; `"paginas"` é quantas páginas
  da categoria ler.
- `"nomes"` acrescenta rótulos amigáveis de aeroporto na mensagem
  (ex.: `{"CGH": "São Paulo / Congonhas"}`).

## Rodar na mão

```bash
python monitor.py --dry-run     # imprime, não envia
python monitor.py --force       # envia mesmo sem promoção
python monitor.py --resumo      # resumo com histórico
python teste_logica.py          # 74 testes offline, sem rede
```

Para experimentar uma janela diferente sem mexer no config de produção:

```bash
python monitor.py --config meu_teste.json --dry-run --force
```

Localmente ele lê as credenciais do arquivo `.env` (copie de `.env.example`).
Esse arquivo está no `.gitignore` e nunca vai para o repositório.

## Histórico

Cada execução acrescenta uma linha em `state/historico.jsonl` e o workflow
faz commit disso. Os pisos são indexados por destino **e data de volta**, e
`melhor` guarda a combinação vencedora com as três bases de preço:

```json
{"quando": "2026-09-16T09:32-03:00",
 "base": "tarifa",
 "pisos": {"Google Flights": {"GIG 22/11": 1255, "GIG 23/11": 1255},
           "Vai de Promo":  {"SDU 22/11": 1351, "SDU 23/11": 967}},
 "melhor": {"preco": 967, "tarifa": 967, "com_taxas": 1148, "total": 1245,
            "fonte": "Vai de Promo", "destino": "SDU",
            "ida": "2026-10-31", "volta": "2026-11-23"},
 "erros": {}}
```

As linhas antigas (sem data no rótulo) continuam sendo lidas nas estatísticas
de 30 dias.

## Limitações conhecidas

- **Fontes não-oficiais**: o Google Flights é lido via [`fast-flights`](https://pypi.org/project/fast-flights/),
  que reconstrói a requisição protobuf do site, o Vai de Promo e o Passagens
  Promo pelas APIs JSON dos próprios buscadores deles. Nenhuma é API
  contratada: se mudarem o formato, quebra — o monitor detecta e manda um
  Telegram de falha em vez de ficar mudo. O mesmo vale para a leitura do HTML
  do Melhores Destinos. Como as fontes são independentes, uma quebrar não
  derruba as outras; o blog, se falhar, só aparece no log.
- **Bloqueio de IP**: runners do GitHub usam IPs de datacenter. Até hoje funciona,
  mas se começar a falhar, configure o secret `PROXY_URL` com um proxy residencial.
- **Fingerprint TLS**: Vai de Promo, Passagens Promo e Melhores Destinos exigem
  que a requisição *pareça* vir de um navegador (o monitor usa `primp` com
  `impersonate="chrome_145"`); sem isso as três respondem 200 com corpo vazio
  ou 403, mesmo de um IP comum. Não é bloqueio de datacenter como o do
  Skyscanner — é só a marca d'água do TLS.
- **Passagens Promo na conta**: ~6 requisições e ~40 s a mais por data de
  volta (busca, 2–3 leituras da lista de idas, uma volta por companhia).
  O blog é 1 requisição por página.
- **Volume**: com a janela de 7 voltas são ~150 requisições por execução
  (medido: 9 min 38 s), contra ~23 (~2,5 min) da versão de data fixa. O workflow tem
  `timeout-minutes: 45`. É volume de uso pessoal; não aumente a frequência
  junto com a janela.
- **Bases de preço**: o Google não separa tarifa de taxa de embarque, então com
  `"base_preco": "tarifa"` as duas fontes não estão em pé de igualdade — o
  número do Google já vem com taxas. A decomposição na mensagem existe para
  você conferir antes de comprar.
- **O itinerário detalhado é o da ida** — o Google só entrega a volta depois que
  você escolhe o voo de ida. Bagagem despachada não está incluída.
- **GitHub desliga cron** de repositórios sem atividade por 60 dias. Como o
  workflow faz commit do histórico, isso não deve acontecer.
