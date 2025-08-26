
import os
import json
import time
from datetime import datetime
from pytz import timezone
from twilio.rest import Client

def send_whatsapp_message(to_number: str, from_number: str, message: str) -> None:
    client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    client.messages.create(to=to_number, from_=from_number, body=message)

def send_template_message(to_wpp: str, from_wpp: str, content_sid: str, vars_dict: dict) -> None:
    client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    client.messages.create(
        to=to_wpp,
        from_=from_wpp,
        content_sid=content_sid,
        content_variables=json.dumps(vars_dict)
    )

def build_headline_message(category: str, headlines: list, max_articles: int = 5) -> str:
    top_articles = headlines[:max_articles]
    formatted = "\n".join([f"• {item}" for item in top_articles])
    return f"🗞️ {category} — Principais manchetes:\n\n{formatted}"

def daily_job(send_message: bool = False) -> None:
    categories = {
        "Politica": [
            "Congresso aprova novo marco fiscal",
            "Ministro defende reforma tributária ainda este ano",
            "Eleições municipais ganham força nos bastidores dos partidos",
            "Comissão discute regulação das redes sociais",
            "Presidente sanciona lei anticorrupção"
        ],
        "Economia": [
            "Inflação fecha julho com alta de 0,2%",
            "Copom mantém taxa Selic em 10,5%",
            "Mercado revê crescimento do PIB para cima",
            "Dólar fecha em queda após dados positivos dos EUA",
            "Desemprego atinge menor nível desde 2015"
        ]
    }

    msg_politica = build_headline_message("Política", categories.get("Politica", []))
    msg_economia = build_headline_message("Economia", categories.get("Economia", []))

    msg_politica = (msg_politica[:730] + "…") if len(msg_politica) > 750 else msg_politica
    msg_economia = (msg_economia[:730] + "…") if len(msg_economia) > 750 else msg_economia

    to_wpp = os.getenv("TWILIO_TO_NUMBER")
    from_wpp = os.getenv("TWILIO_FROM_NUMBER")
    tpl_sid = os.getenv("CONTENT_SID_DAILY")

    today_str = datetime.now(timezone("America/Sao_Paulo")).strftime("%d/%m/%Y")

    if send_message:
        if tpl_sid:
            send_template_message(to_wpp, from_wpp, tpl_sid, {"1": today_str, "2": "Política"})
            time.sleep(1)
            send_whatsapp_message(to_wpp, from_wpp, msg_politica)

            time.sleep(1)
            send_template_message(to_wpp, from_wpp, tpl_sid, {"1": today_str, "2": "Economia"})
            time.sleep(1)
            send_whatsapp_message(to_wpp, from_wpp, msg_economia)
        else:
            send_whatsapp_message(to_wpp, from_wpp, msg_politica)
            time.sleep(1)
            send_whatsapp_message(to_wpp, from_wpp, msg_economia)
