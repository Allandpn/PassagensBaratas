# Monitor de passagens — Florianópolis ⇄ Rio de Janeiro

Roda de madrugada na nuvem (GitHub Actions), varre o Google Flights e manda um
alerta no Telegram quando a ida e volta cai para a faixa que você quer.

| Trecho | Datas | Alvo |
|---|---|---|
| FLN ⇄ RIO (GIG + SDU), 1 pessoa, econômica | 31/10/2026 → 22/11/2026 | 🔥🔥 < R$ 800 · 🔥 R$ 800–900 · ⚠️ R$ 900–1000 |

## Como ele acha preço que o Google esconde

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
| Nenhum destino respondeu | ⚠️ avisa que o monitor quebrou |

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
{"quando": "2026-09-15T18:47-03:00", "pisos": {"RIO": 1306, "GIG": 1427, "SDU": 1306}, "erros": {}}
```

## Limitações conhecidas

- **Fonte**: Google Flights via [`fast-flights`](https://pypi.org/project/fast-flights/),
  que reconstrói a requisição protobuf do site. Se o Google mudar o formato, a lib
  precisa ser atualizada — o monitor detecta isso e manda um Telegram de falha.
- **Bloqueio de IP**: runners do GitHub usam IPs de datacenter. Até hoje funciona,
  mas se começar a falhar, configure o secret `PROXY_URL` com um proxy residencial.
- **Volume**: ~15 requisições por execução, 5 execuções por dia. É volume de uso
  pessoal; não aumente a frequência sem necessidade.
- **Preço mostrado é o total ida + volta** da tarifa econômica. O itinerário
  detalhado na mensagem é o da **ida** — o Google só entrega a volta depois que
  você escolhe o voo de ida. Bagagem despachada não está incluída.
- **GitHub desliga cron** de repositórios sem atividade por 60 dias. Como o
  workflow faz commit do histórico, isso não deve acontecer.
