"""
Agente de deteção de MOVIMENTO DE ODDS (steam vs drift) em futebol e ténis.

O QUE FAZ
---------
Cada vez que corres este script, ele:
1. Vai buscar as odds atuais a vários bookmakers (região Europa).
2. Compara com as odds da última vez que correu (guardadas num ficheiro
   local `estado_odds.json`).
3. Para cada odd que mudou mais do que MIN_MOVE_PERCENT desde a última
   verificação, regista um "movimento".
4. Classifica cada movimento:
   - STEAM  = 2 ou mais bookmakers moveram a mesma odd, na mesma direção,
     dentro da mesma janela de tempo -> sinal mais forte, geralmente
     associado a dinheiro informado/profissional a entrar rápido.
   - DRIFT  = só 1 bookmaker moveu -> normalmente é só o mercado
     recreativo a ajustar-se lentamente, sinal mais fraco.
5. Regista tudo em `alertas_movimento.csv` e imprime os alertas no ecrã.

IMPORTANTE SOBRE O FICHEIRO DE ESTADO
--------------------------------------
`estado_odds.json` guarda a "fotografia" das odds da última execução.
NÃO apagues este ficheiro entre execuções — é ele que permite comparar
"agora" com "há pouco". Se o apagares, o script começa do zero (sem nada
para comparar na primeira vez a seguir).

COMO CORRER
-----------
1. Cola a tua API key abaixo (a mesma do outro script).
2. pip install requests   (só precisas de fazer isto uma vez)
3. python agente_odds_movimento.py
4. Corre outra vez passado uns minutos para veres a comparação em ação —
   a primeira execução nunca tem alertas (não há nada anterior para comparar).

CONFIGURAÇÃO
------------
Ajusta as variáveis na secção CONFIG abaixo consoante o que quiseres.
"""

import os
import csv
import json
from datetime import datetime, timezone

import requests

# ───────────────────────── CONFIG ─────────────────────────

API_KEY = os.environ.get("ODDS_API_KEY", "COLA_AQUI_A_TUA_API_KEY")

# Grupos de desportos a monitorizar (a lista de campeonatos ativos dentro
# destes grupos é descoberta automaticamente a cada execução).
GRUPOS_DESPORTO = ["Soccer", "Tennis"]

MARKET = "h2h"      # vencedor do evento
REGIONS = "eu"       # bookmakers da região Europa

# Variação mínima (%) para contar como "movimento" digno de alerta.
MIN_MOVE_PERCENT = 10.0

# Eventos com menos casas de apostas listadas tendem a ter odds mais erráticas
# (menos liquidez = mais ruído). Ignoramos eventos abaixo deste mínimo.
MIN_BOOKMAKERS_POR_EVENTO = 3

# Odds abaixo disto são pouco plausíveis em mercados pré-jogo normais e
# tendem a ser erros pontuais do fornecedor de dados, não movimento real.
# Também não seriam "acionáveis" de qualquer forma (margem quase nula).
ODD_MINIMA_VALIDA = 1.15

# Se a última odd guardada para essa combinação evento+bookmaker+resultado
# tiver mais do que isto (em minutos), não é usada para comparar — trata-se
# como se fosse a primeira vez a ver essa odd (evita comparar com dados
# demasiado velhos, ex: de há vários dias).
MAX_IDADE_MINUTOS = 120

STATE_FILE = "estado_odds.json"
LOG_FILE = "alertas_movimento.csv"

# Telegram: o bot token vem do BotFather, o chat_id é o teu ID pessoal de chat.
# Em produção (GitHub Actions), estes valores vêm de variáveis de ambiente/secrets,
# nunca escritos diretamente no código.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "COLA_AQUI_O_TEU_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "COLA_AQUI_O_TEU_CHAT_ID")

# Envia uma mensagem de confirmação a cada execução, mesmo sem alertas.
# Útil agora para confirmares a ligação ao Telegram; depois de confirmado,
# e principalmente quando isto correr automaticamente de x em x minutos,
# muda para False (senão recebes uma mensagem a cada execução, o que é spam).
ENVIAR_HEARTBEAT = True

# ────────────────────────────────────────────────────────────


def enviar_telegram(mensagem: str):
    """Envia uma mensagem de texto para o teu chat via bot do Telegram."""
    if TELEGRAM_BOT_TOKEN == "COLA_AQUI_O_TEU_TOKEN" or TELEGRAM_CHAT_ID == "COLA_AQUI_O_TEU_CHAT_ID":
        print("[aviso] Telegram não configurado (token ou chat_id em falta) — só a imprimir no ecrã.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": mensagem}, timeout=10)
        corpo = resp.json()
        if not corpo.get("ok"):
            print(f"[aviso] Telegram recusou a mensagem: {corpo}")
        else:
            print("[info] mensagem enviada ao Telegram com sucesso.")
    except Exception as e:
        print(f"[aviso] falha ao enviar Telegram: {e}")


def descobrir_campeonatos_ativos() -> list:
    """Devolve as sport_keys atualmente ativas dentro dos GRUPOS_DESPORTO."""
    url = "https://api.the-odds-api.com/v4/sports/"
    params = {"apiKey": API_KEY, "all": "false"}
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code != 200:
        print(f"[erro] não consegui listar desportos ativos: {resp.status_code} {resp.text[:200]}")
        return []
    todos = resp.json()
    ativos = [d["key"] for d in todos if d.get("group") in GRUPOS_DESPORTO and d.get("active")]
    print(f"Campeonatos ativos encontrados agora: {len(ativos)} -> {ativos}")
    return ativos


def buscar_odds(sport: str) -> list:
    """Vai buscar odds atuais para um desporto à The Odds API."""
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
    params = {
        "apiKey": API_KEY,
        "regions": REGIONS,
        "markets": MARKET,
        "oddsFormat": "decimal",
    }
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code != 200:
        print(f"[aviso] falha ao consultar {sport}: {resp.status_code} {resp.text[:200]}")
        return []
    return resp.json()


def carregar_estado() -> dict:
    if os.path.isfile(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def guardar_estado(estado: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def registar_alerta(nome_evento: str, resultado: str, tipo: str, detalhes: str):
    linha_existe = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not linha_existe:
            writer.writerow(["timestamp", "evento", "resultado", "tipo", "detalhes"])
        writer.writerow([datetime.now(timezone.utc).isoformat(), nome_evento, resultado, tipo, detalhes])


def main():
    if API_KEY == "COLA_AQUI_A_TUA_API_KEY":
        print("⚠️  Define a tua API key em API_KEY ou na variável de ambiente ODDS_API_KEY.")
        return

    estado_anterior = carregar_estado()
    estado_novo = {}
    agora = datetime.now(timezone.utc)

    campeonatos = descobrir_campeonatos_ativos()
    if not campeonatos:
        print("Nenhum campeonato ativo encontrado em Soccer/Tennis neste momento.")
        return

    total_movimentos = 0
    casas_vistas = set()

    for sport in campeonatos:
        eventos = buscar_odds(sport)
        print(f"[{sport}] {len(eventos)} eventos encontrados")

        for evento in eventos:
            nome_evento = f"{evento.get('home_team')} vs {evento.get('away_team')}"
            event_id = evento["id"]

            # Ignora jogos já em curso — odds "ao vivo" mudam por causa do
            # tempo/marcador, não por informação de mercado, e contaminam a
            # deteção de steam/drift pré-jogo.
            commence = datetime.fromisoformat(evento["commence_time"].replace("Z", "+00:00"))
            if commence <= agora:
                continue

            if len(evento.get("bookmakers", [])) < MIN_BOOKMAKERS_POR_EVENTO:
                continue  # pouca liquidez, ignora este evento

            # movimentos_por_resultado: resultado -> lista de (bookmaker, direção, pct)
            movimentos_por_resultado = {}

            for bookmaker in evento.get("bookmakers", []):
                casas_vistas.add(bookmaker["title"])
                for mercado in bookmaker.get("markets", []):
                    if mercado["key"] != MARKET:
                        continue
                    for outcome in mercado["outcomes"]:
                        resultado = outcome["name"]
                        odd_atual = outcome["price"]

                        if odd_atual < ODD_MINIMA_VALIDA:
                            continue  # provável erro de dados, ignora esta leitura

                        chave = f"{sport}|{event_id}|{bookmaker['key']}|{resultado}"
                        estado_novo[chave] = {
                            "odd": odd_atual,
                            "timestamp": agora.isoformat(),
                            "evento": nome_evento,
                            "resultado": resultado,
                            "bookmaker": bookmaker["title"],
                        }

                        anterior = estado_anterior.get(chave)
                        if not anterior:
                            continue  # primeira vez que vemos esta combinação

                        idade_min = (agora - datetime.fromisoformat(anterior["timestamp"])).total_seconds() / 60
                        if idade_min > MAX_IDADE_MINUTOS:
                            continue  # dados velhos demais, não compara

                        odd_anterior = anterior["odd"]
                        if odd_anterior < ODD_MINIMA_VALIDA:
                            continue  # valor anterior já era um provável erro, não comparar
                        variacao_pct = (odd_atual - odd_anterior) / odd_anterior * 100

                        if abs(variacao_pct) >= MIN_MOVE_PERCENT:
                            direcao = "subida" if variacao_pct > 0 else "queda"
                            movimentos_por_resultado.setdefault(resultado, []).append(
                                (bookmaker["title"], direcao, variacao_pct, odd_anterior, odd_atual)
                            )

            # classificar cada resultado com movimento: STEAM vs DRIFT
            for resultado, movimentos in movimentos_por_resultado.items():
                # conta quantas casas moveram para cada direção
                contagem_direcao = {}
                for m in movimentos:
                    direcao = m[1]
                    contagem_direcao[direcao] = contagem_direcao.get(direcao, 0) + 1

                direcao_maioritaria, casas_na_maioria = max(contagem_direcao.items(), key=lambda kv: kv[1])

                # STEAM = 2+ casas concordam na mesma direção (mesmo que 1 discorde)
                if casas_na_maioria >= 2:
                    tipo = "STEAM"
                    icone = "🔥"
                else:
                    tipo = "DRIFT"
                    icone = "〰️"

                total_movimentos += 1
                print(f"\n{icone} {tipo} — {nome_evento} — resultado: {resultado}")
                detalhes_lista = []
                for casa, direcao, pct, odd_ant, odd_atu in movimentos:
                    linha = f"{casa}: {odd_ant} -> {odd_atu} ({direcao} {abs(pct):.1f}%)"
                    print(f"   {linha}")
                    detalhes_lista.append(linha)

                registar_alerta(nome_evento, resultado, tipo, " | ".join(detalhes_lista))

                if tipo == "STEAM":
                    mensagem_telegram = (
                        f"{icone} {tipo} — {nome_evento}\n"
                        f"Resultado: {resultado}\n" + "\n".join(detalhes_lista)
                    )
                    enviar_telegram(mensagem_telegram)

    guardar_estado(estado_novo)
    print(f"\nConcluído. {total_movimentos} movimento(s) de odds registado(s) em {LOG_FILE}.")
    print("(Se isto foi a primeira execução, é normal teres 0 — não há nada anterior para comparar ainda.)")
    print(f"\nCasas de apostas encontradas nesta execução ({len(casas_vistas)}): {sorted(casas_vistas)}")
    if any("22" in c or "22bet" in c.lower() for c in casas_vistas):
        print("✅ A 22Bet parece estar coberta por esta API.")
    else:
        print("⚠️  A 22Bet NÃO apareceu na lista acima — provavelmente não é coberta por esta API.")

    # Heartbeat: confirma que o script correu e que o Telegram está ligado,
    # mesmo que não tenha havido nenhum STEAM para alertar.
    hora = agora.strftime("%Y-%m-%d %H:%M UTC")
    if ENVIAR_HEARTBEAT:
        enviar_telegram(
            f"✅ Agente correu às {hora}\n"
            f"{len(campeonatos)} campeonatos verificados, {total_movimentos} movimento(s) encontrado(s)."
        )


if __name__ == "__main__":
    main()
