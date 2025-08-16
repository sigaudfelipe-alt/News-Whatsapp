#!/usr/bin/env python3
"""
Agente de cardápio semanal com envio por WhatsApp
=================================================

Este script é uma variação do agente de cardápio semanal que,
em vez de enviar o cardápio por e‑mail, envia a mensagem via
WhatsApp usando a API do Twilio.  Para usar este agente você
precisa:

* Uma conta na Twilio (pode ser gratuita) e acesso ao sandbox
  de WhatsApp, ou um número de WhatsApp habilitado pela Twilio.
* As credenciais **Account SID** e **Auth Token** da sua conta
  Twilio.
* O número de WhatsApp da Twilio (sandbox ou número aprovado), no
  formato `whatsapp:+14155238886`.
* O número de WhatsApp do destinatário no formato internacional,
  prefixado com `whatsapp:+`.  No sandbox, o número precisa ser
  previamente autorizado enviando o código de adesão ao número de
  teste.

Defina as seguintes variáveis de ambiente antes de executar o
script:

```
export TWILIO_ACCOUNT_SID="ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
export TWILIO_AUTH_TOKEN="seu-auth-token"
export TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886"
export WHATSAPP_RECIPIENT="whatsapp:+5511999999999"
```

Instale as dependências necessárias:

```sh
pip install requests beautifulsoup4 schedule twilio
```

O restante do funcionamento é idêntico ao script original: ele
coleta receitas do post "Top 13: cardápios para resolver o jantar da
semana", sorteia sete pratos, extrai os ingredientes via JSON‑LD da
página da receita e envia uma mensagem com o cardápio e a lista de
compras todo domingo às 08h00 (horário local).
"""

import os
import json
import random
import time
from typing import List, Tuple

import requests
import schedule
from bs4 import BeautifulSoup
from twilio.rest import Client

# URL do blog com cardápios para o jantar da semana
BLOG_URL: str = (
    "https://panelinha.com.br/blog/ritalobo/post/top-13-cardapios-para-resolver-o-jantar-da-semana"
)

def get_recipe_urls() -> List[str]:
    """Extrai todas as URLs de receitas do post do blog.

    A página contém várias listas de menus; procuramos por links que
    apontem para o domínio ``panelinha.com.br/receita``.  Links
    relativos (``/receita/``) são convertidos em URLs absolutas.
    """
    resp = requests.get(BLOG_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.find_all("a", href=True)
    recipe_urls: List[str] = []
    for a in anchors:
        href = a['href']
        if href.startswith("https://www.panelinha.com.br/receita/"):
            recipe_urls.append(href)
        elif href.startswith("/receita/"):
            recipe_urls.append(f"https://www.panelinha.com.br{href}")
    # Remove duplicidades mantendo a ordem
    return list(dict.fromkeys(recipe_urls))

def parse_recipe(url: str) -> Tuple[str, List[str]]:
    """Extrai o nome e a lista de ingredientes de uma receita.

    Procura o script JSON‑LD (id ``js_recipe_schema``) para ler o
    campo ``recipeIngredient``.  Se não for encontrado, tenta
    extrair listas ``li`` como fallback.
    """
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title_tag = soup.find("title")
    recipe_name = title_tag.get_text(strip=True) if title_tag else url
    script_tag = soup.find("script", id="js_recipe_schema")
    ingredients: List[str] = []
    if script_tag and script_tag.string:
        try:
            data = json.loads(script_tag.string)
            recipe_name = data.get("name", recipe_name)
            ingredients = data.get("recipeIngredient", [])
        except json.JSONDecodeError:
            ingredients = []
    if not ingredients:
        # Fallback: procura listas de ingredientes no HTML
        for h in soup.find_all(['h2', 'h3', 'h4', 'h5']):
            if 'Ingrediente' in h.get_text():
                ul = h.find_next('ul')
                if ul:
                    ingredients.extend([li.get_text(strip=True) for li in ul.find_all('li')])
        if not ingredients:
            for li in soup.find_all('li'):
                text = li.get_text(strip=True)
                if text:
                    ingredients.append(text)
    return recipe_name, ingredients

def build_menu() -> Tuple[List[Tuple[str, str]], List[str]]:
    """Gera um cardápio com sete receitas aleatórias e a lista de compras."""
    urls = get_recipe_urls()
    if len(urls) < 7:
        raise RuntimeError("Não há receitas suficientes no post para sortear.")
    selected = random.sample(urls, 7)
    menu: List[Tuple[str, str]] = []
    all_ingredients: List[str] = []
    for url in selected:
        try:
            name, ingredients = parse_recipe(url)
        except Exception as exc:
            print(f"Falha ao ler {url}: {exc}")
            continue
        menu.append((name, url))
        all_ingredients.extend(ingredients)
    # Deduplicação dos ingredientes (ignorando caixa)
    seen = {}
    for item in all_ingredients:
        key = item.strip().lower()
        if key not in seen:
            seen[key] = item.strip()
    ingredients_list = sorted(seen.values(), key=lambda s: s.lower())
    return menu, ingredients_list

def compose_message(menu: List[Tuple[str, str]], ingredients: List[str]) -> str:
    """Monta o texto da mensagem para envio pelo WhatsApp."""
    dias = [
        "Segunda-feira",
        "Terça-feira",
        "Quarta-feira",
        "Quinta-feira",
        "Sexta-feira",
        "Sábado",
        "Domingo",
    ]
    linhas: List[str] = []
    linhas.append("Olá! Aqui está o cardápio semanal sugerido:\n")
    for idx, (nome, url) in enumerate(menu):
        dia = dias[idx % len(dias)]
        linhas.append(f"{dia}: {nome} — {url}")
    linhas.append("\nLista de compras:")
    for item in ingredients:
        linhas.append(f"- {item}")
    return "\n".join(linhas)

def send_whatsapp(body: str) -> None:
    """Envia uma mensagem de texto via WhatsApp usando a API da Twilio.

    Requer as seguintes variáveis de ambiente:

    - ``TWILIO_ACCOUNT_SID``
    - ``TWILIO_AUTH_TOKEN``
    - ``TWILIO_WHATSAPP_NUMBER`` (número do sandbox/número de envio)
    - ``WHATSAPP_RECIPIENT`` (número de destino)
    """
    account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
    auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
    from_whatsapp = os.environ.get('TWILIO_WHATSAPP_NUMBER')
    to_whatsapp = os.environ.get('WHATSAPP_RECIPIENT')
    missing = []
    if not account_sid:
        missing.append('TWILIO_ACCOUNT_SID')
    if not auth_token:
        missing.append('TWILIO_AUTH_TOKEN')
    if not from_whatsapp:
        missing.append('TWILIO_WHATSAPP_NUMBER')
    if not to_whatsapp:
        missing.append('WHATSAPP_RECIPIENT')
    if missing:
        raise RuntimeError(f"Variáveis de ambiente ausentes: {', '.join(missing)}")
    client = Client(account_sid, auth_token)
    message = client.messages.create(
        body=body,
        from_=from_whatsapp,
        to=to_whatsapp,
    )
    print(f"Mensagem enviada: SID {message.sid}")

def job() -> None:
    """Tarefa agendada: monta o cardápio e envia pelo WhatsApp."""
    print("Gerando cardápio...")
    menu, ingredients = build_menu()
    texto = compose_message(menu, ingredients)
    try:
        send_whatsapp(texto)
    except Exception as exc:
        print(f"Erro ao enviar mensagem: {exc}")
    else:
        print("Cardápio enviado via WhatsApp!")

def schedule_job() -> None:
    """Agenda o envio todo domingo às 08:00 (horário local)."""
    schedule.every().sunday.at("08:00").do(job)
    print("Agente de cardápio via WhatsApp iniciado.")
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == '__main__':
    schedule_job()