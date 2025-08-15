#!/usr/bin/env python3
"""
Agente de cardápio semanal
==========================

Este script implementa um agente que consulta o site Panelinha para
montar um cardápio semanal e enviar a lista de compras.  **Versão
WhatsApp**: nesta versão, a mensagem é enviada por WhatsApp usando a
API da Twilio em vez de e‑mail.  O agente funciona em três etapas
principais:

1. **Coleta de receitas** – A função `get_recipe_urls` acessa o post
   “Top 13: cardápios para resolver o jantar da semana” e extrai todas
   as URLs de receitas.  Essa página contém uma variedade de menus
   sugeridos pela Rita Lobo e, como as URLs ficam em links estáticos
   (âmbito do HTML, não dependente de JavaScript), é possível
   encontrá‑las com o BeautifulSoup.

2. **Montagem do cardápio** – A função `build_menu` sorteia sete
   receitas diferentes, uma para cada dia da semana.  Para cada
   receita, `parse_recipe` carrega a página da receita individual e
   procura pelo script `js_recipe_schema`, que contém um objeto JSON
   com a lista de ingredientes.  Esse método evita depender de
   estruturas de HTML propensas a mudar e permite capturar todos os
   ingredientes declarados na receita【147936079420209†L60-L80】.  As listas
   de ingredientes de todas as receitas sorteadas são combinadas e
   deduplicadas para formar uma lista de compras.

3. **Envio da mensagem** – A função `send_whatsapp` usa a biblioteca
   `twilio` para enviar um texto via WhatsApp contendo o cardápio e a
   lista de compras.  Para preservar a privacidade, as credenciais da
   Twilio (`TWILIO_ACCOUNT_SID` e `TWILIO_AUTH_TOKEN`), o número de
   envio (`TWILIO_WHATSAPP_NUMBER`) e o número de destino
   (`WHATSAPP_RECIPIENT`) devem ser definidos via variáveis de
   ambiente.  A função `schedule_job` agenda a execução do agente todo
   domingo às 08:00 (horário local).  Ao ser executado, o script
   entra em um loop que verifica se há tarefas pendentes a cada
   minuto.

Antes de executar, instale as dependências com:

```sh
pip install requests beautifulsoup4 schedule twilio
```

E defina as variáveis de ambiente com as credenciais da Twilio e os
números de WhatsApp.  Por exemplo:

```sh
export TWILIO_ACCOUNT_SID="ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
export TWILIO_AUTH_TOKEN="seu-auth-token"
export TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886"
export WHATSAPP_RECIPIENT="whatsapp:+5511999999999"
```

Para iniciar o agente imediatamente, execute:

```sh
python3 meal_planner.py
```

O script permanecerá em execução, e a cada domingo às 08:00 enviará
automaticamente o cardápio semanal e a lista de compras.
"""

import os
import json
import random
import time
import requests
import schedule
from typing import List, Tuple
from bs4 import BeautifulSoup
from twilio.rest import Client

# URL do blog com cardápios para o jantar da semana
BLOG_URL: str = (
    "https://panelinha.com.br/blog/ritalobo/post/top-13-cardapios-para-resolver-o-jantar-da-semana"
)

def get_recipe_urls() -> List[str]:
    """Retorna uma lista com todas as URLs de receitas encontradas no post do blog.

    A página contém vários menus, cada um com links para receitas.  Ao
    procurar todas as âncoras cujo href começa com
    `https://www.panelinha.com.br/receita/`, coletamos todos esses
    endereços.  Para evitar repetições, convertemos a lista para um
    dicionário e de volta para lista.
    """
    resp = requests.get(BLOG_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.find_all("a", href=True)
    recipe_urls: List[str] = []
    for a in anchors:
        href = a['href']
        # Alguns links da página já vêm com domínio completo (https://www.panelinha.com.br/receita/...),
        # outros podem estar começando com "/receita/".  Adicionamos o domínio se necessário.
        if href.startswith("https://www.panelinha.com.br/receita/"):
            recipe_urls.append(href)
        elif href.startswith("/receita/"):
            recipe_urls.append(f"https://www.panelinha.com.br{href}")
    # Remover duplicidades preservando ordem
    unique_urls = list(dict.fromkeys(recipe_urls))
    return unique_urls

def parse_recipe(url: str) -> Tuple[str, List[str]]:
    """Extrai o nome da receita e a lista de ingredientes da página.

    A função procura o script com id ``js_recipe_schema``, que contém um
    JSON-LD com os campos ``recipeIngredient`` e ``name``.  Se esse
    script não existir, tenta extrair as listas ``li`` sob o título
    “Ingredientes”.  O retorno é uma tupla com o nome da receita e
    uma lista de ingredientes em texto.
    """
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    # Pega o título da página como fallback para o nome da receita
    title_tag = soup.find("title")
    recipe_name = title_tag.get_text(strip=True) if title_tag else url
    # Procura o script JSON-LD com ingredientes
    script_tag = soup.find("script", id="js_recipe_schema")
    ingredients: List[str] = []
    if script_tag and script_tag.string:
        try:
            data = json.loads(script_tag.string)
            # Alguns campos podem ser maiúsculos; usamos get para evitar erros
            recipe_name = data.get("name", recipe_name)
            ingredients = data.get("recipeIngredient", [])
        except json.JSONDecodeError:
            # Se o JSON estiver malformatado, ignora e tenta extrair manualmente
            ingredients = []
    if not ingredients:
        # Fallback: procura listas de ingredientes no HTML
        for h in soup.find_all(['h2', 'h3', 'h4', 'h5']):
            if 'Ingrediente' in h.get_text():
                ul = h.find_next('ul')
                if ul:
                    for li in ul.find_all('li'):
                        text = li.get_text(strip=True)
                        if text:
                            ingredients.append(text)
        # Se ainda estiver vazio, captura todos elementos <li>
        if not ingredients:
            for li in soup.find_all('li'):
                text = li.get_text(strip=True)
                if text:
                    ingredients.append(text)
    return recipe_name, ingredients

def build_menu() -> Tuple[List[Tuple[str, str]], List[str]]:
    """Sorteia sete receitas e retorna o cardápio e a lista de compras.

    Seleciona aleatoriamente sete URLs da lista de receitas do blog.
    Para cada receita, chama ``parse_recipe`` para obter o nome e os
    ingredientes.  Em seguida, concatena todas as listas de ingredientes e
    deduplica (ignorando diferenças entre maiúsculas/minúsculas).  O
    resultado é uma lista de tuplas (nome, url) e uma lista de
    ingredientes únicos ordenados alfabeticamente.
    """
    urls = get_recipe_urls()
    if len(urls) < 7:
        raise RuntimeError(
            "Não foram encontradas receitas suficientes para montar o cardápio."
        )
    selected = random.sample(urls, 7)
    menu: List[Tuple[str, str]] = []
    all_ingredients: List[str] = []
    for url in selected:
        try:
            name, ingredients = parse_recipe(url)
        except Exception as exc:
            # Se houver problema com a receita, pula para a próxima
            print(f"Falha ao processar {url}: {exc}")
            continue
        menu.append((name, url))
        all_ingredients.extend(ingredients)
    # Deduplicação simples ignorando acentos e caixa
    normalized = {}
    for item in all_ingredients:
        key = item.strip().lower()
        if key not in normalized:
            normalized[key] = item.strip()
    unique_ingredients = sorted(normalized.values(), key=lambda s: s.lower())
    return menu, unique_ingredients

def compose_message(menu: List[Tuple[str, str]], ingredients: List[str]) -> str:
    """Gera a mensagem de WhatsApp com o cardápio e a lista de compras.

    A mensagem inclui um cumprimento, o nome de cada dia da semana seguido da
    receita (com link), e uma lista de compras deduplicada.  Essa função
    retorna apenas o corpo da mensagem, pois o WhatsApp não possui campos
    separados de assunto e corpo como um e‑mail tradicional.
    """
    dias_semana = [
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
        dia = dias_semana[idx % len(dias_semana)]
        linhas.append(f"{dia}: {nome} — {url}")
    linhas.append("\nLista de compras:")
    for item in ingredients:
        linhas.append(f"- {item}")
    return "\n".join(linhas)

def send_whatsapp(body: str) -> None:
    """Envia uma mensagem de WhatsApp usando a API da Twilio.

    As credenciais de autenticação são lidas das variáveis de ambiente
    `TWILIO_ACCOUNT_SID` e `TWILIO_AUTH_TOKEN`.  O número de origem do
    WhatsApp (por exemplo, o número do sandbox) deve estar em
    `TWILIO_WHATSAPP_NUMBER`, e o número de destino em `WHATSAPP_RECIPIENT`.
    Todos os números devem incluir o prefixo "whatsapp:" seguido do
    número em formato internacional (ex.: "whatsapp:+14155238886").
    """
    account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
    auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
    from_whatsapp = os.environ.get('TWILIO_WHATSAPP_NUMBER')
    to_whatsapp = os.environ.get('WHATSAPP_RECIPIENT')
    if not account_sid or not auth_token or not from_whatsapp or not to_whatsapp:
        raise RuntimeError(
            "Credenciais ou números ausentes. Configure as variáveis de "
            "ambiente TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER "
            "e WHATSAPP_RECIPIENT."
        )
    client = Client(account_sid, auth_token)
    client.messages.create(body=body, from_=from_whatsapp, to=to_whatsapp)

def job() -> None:
    """Tarefa agendada que monta o menu e envia a mensagem de WhatsApp."""
    print("Construindo cardápio...")
    menu, ingredients = build_menu()
    corpo = compose_message(menu, ingredients)
    try:
        send_whatsapp(body=corpo)
        print("Cardápio enviado com sucesso via WhatsApp!")
    except Exception as exc:
        print(f"Falha ao enviar a mensagem de WhatsApp: {exc}")

def schedule_job() -> None:
    """Agenda a execução do job todo domingo às 08:00, horário local.

    A função `job` monta o cardápio e envia a mensagem via WhatsApp, então
    este agendamento garante que o cardápio semanal seja enviado
    automaticamente no horário desejado.
    """
    schedule.every().sunday.at("08:00").do(job)
    print("Agente de cardápio iniciado. Aguardando o horário programado...")
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == '__main__':
    schedule_job()