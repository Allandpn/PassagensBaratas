# Monitor de passagens — Florianópolis ⇄ Rio de Janeiro

Roda de madrugada na nuvem (GitHub Actions), varre o Google Flights e o Vai de
Promo, e manda um alerta no Telegram quando a ida e volta cai para a faixa que
você quer.

| Trecho | Datas | Alvo |
|---|---|---|
| FLN ⇄ RIO (GIG + SDU), 1 pessoa, econômica | 31/10/2026 → 22/11/2026 | 🔥🔥 < R$ 800 · 🔥 R$ 800–900 · ⚠️ R$ 900–1000 |

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

GET flights.vaidepromo.com.br/api/search/FLNGIG261031-GIGFLN261122/1/0/0/Y/G3
    → {"prices":[...], "recommendations":[...], "itineraries":[[ida],[volta]]}
```

O token de ida e volta usa **hífen** entre os trechos (`FLNGIG261031-GIGFLN261122`);
sem ele a API devolve HTTP 500. Permitido pelo `robots.txt` deles — só
`/redirect/`, `/safearea/` e as páginas de pagamento são `Disallow`.

Traz preço já com taxas e costuma listar **voo direto** que o Google não mostra
(ex.: Gol 1963 FLN→GIG sem escala). Em compensação sai mais caro: no mesmo
instante, R$ 1.666 contra R$ 1.306 do Google. Ter as duas fontes é justamente
o ponto — cada uma pega promoção que a outra perde.

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
| `PROXY_URL` | *(opcional)* proxy, caso o Google bloqueie o IP do runner |

**4. Testar**

Aba **Actions → Monitor de voos FLN <-> Rio → Run workflow**. Deixe
"Mandar mensagem mesmo sem promoção" marcado — deve chegar um Telegram em ~2 min.

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

## Ajustes

Tudo em `config.json`:

```json
"alvos":          { "jackpot": 800, "alerta": 900, "aviso": 1000 },
"degraus_preco":  [800, 900, 1000, 1200],
"antispam_horas": 12
```

- Quer ser avisado até R$ 1.200? Troque `"aviso": 1000` por `1200` e inclua
  `1200` nos degraus.
- Datas flexíveis: mude `data_ida` / `data_volta`.
- Menos consultas por rodada: deixe `"destinos": ["RIO"]` (o código `RIO` já
  cobre Galeão e Santos Dumont; GIG e SDU separados só melhoram a cobertura).
- `"destinos"` são as consultas no Google; `"destinos_vaidepromo"` as do Vai de
  Promo, que não aceita código de cidade — só aeroporto (`GIG`, `SDU`).
  Deixe `"destinos_vaidepromo": []` para desligar essa fonte.

## Rodar na mão

```bash
python monitor.py --dry-run     # imprime, não envia
python monitor.py --force       # envia mesmo sem promoção
python monitor.py --resumo      # resumo com histórico
```

Localmente ele lê as credenciais do arquivo `.env` (copie de `.env.example`).
Esse arquivo está no `.gitignore` e nunca vai para o repositório.

## Histórico

Cada execução acrescenta uma linha em `state/historico.jsonl` e o workflow
faz commit disso. Com o tempo dá para ver a curva de preço da rota:

```json
{"quando": "2026-09-15T19:30-03:00",
 "pisos": {"Google Flights": {"RIO": 1306, "GIG": 1427, "SDU": 1306},
           "Vai de Promo":  {"GIG": 1666, "SDU": 1694}},
 "erros": {}}
```

## Limitações conhecidas

- **Fontes não-oficiais**: o Google Flights é lido via [`fast-flights`](https://pypi.org/project/fast-flights/),
  que reconstrói a requisição protobuf do site, e o Vai de Promo pela API JSON
  do buscador deles. Nenhuma das duas é API contratada: se mudarem o formato,
  quebra — o monitor detecta e manda um Telegram de falha em vez de ficar mudo.
  Como são duas fontes independentes, uma quebrar não derruba o monitor.
- **Bloqueio de IP**: runners do GitHub usam IPs de datacenter. Até hoje funciona,
  mas se começar a falhar, configure o secret `PROXY_URL` com um proxy residencial.
- **Volume**: ~23 requisições por execução (~2,5 min), 5 execuções por dia. É
  volume de uso pessoal; não aumente a frequência sem necessidade.
- **Preço mostrado é o total ida + volta** da tarifa econômica. O itinerário
  detalhado na mensagem é o da **ida** — o Google só entrega a volta depois que
  você escolhe o voo de ida. Bagagem despachada não está incluída.
- **GitHub desliga cron** de repositórios sem atividade por 60 dias. Como o
  workflow faz commit do histórico, isso não deve acontecer.
