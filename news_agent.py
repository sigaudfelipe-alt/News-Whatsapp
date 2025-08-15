"""
news_agent.py

This module implements a news aggregation and WhatsApp delivery agent.  Every
morning at 06:00 (São Paulo time) the agent fetches the latest stories from a
set of RSS feeds (Folha, Estadão, O Globo, The New York Times and the Wall
Street Journal) and headlines scraped from Valor International.  It then sends
a concise Portuguese‑language summary of the day’s most important events to a
WhatsApp number via the Twilio API.  The summarisation is performed using
OpenAI’s chat completions API.

To run this script you need to install the following packages:

```
pip install feedparser requests beautifulsoup4 schedule pytz twilio openai
```

Before running, set the following environment variables in your shell or in a
.env file loaded by python‑dotenv (if you choose to use it):

* `OPENAI_API_KEY` – your OpenAI API key for calling the chat completion endpoint.
* `TWILIO_ACCOUNT_SID` – your Twilio account SID.
* `TWILIO_AUTH_TOKEN` – your Twilio auth token.
* `TWILIO_FROM_NUMBER` – your WhatsApp sending number (e.g. `whatsapp:+14155238886`).
* `TWILIO_TO_NUMBER` – the WhatsApp number that should receive the news summary.

Because running scheduled jobs in long‑running processes can be brittle, you
should consider configuring a system cron job to invoke this script at
06:00 each day.  For example, on a Linux system you might add the following
line to your crontab (edit via `crontab -e`):

```
0 6 * * * /usr/bin/python3 /path/to/news_agent.py >> /var/log/news_agent.log 2>&1
```

Note: This script only prepares the messages; it will not actually send
messages or call external APIs when executed by ChatGPT during analysis.  To
activate the WhatsApp integration you must supply valid Twilio credentials and
uncomment the call to ``send_whatsapp_message`` in the `daily_job` function.
"""

import os
import datetime
import time
import logging
from typing import List, Dict, Optional

import feedparser
import requests
from bs4 import BeautifulSoup
import pytz
import schedule

try:
    import openai
except ImportError:
    openai = None  # openai is optional during development

try:
    from twilio.rest import Client
except ImportError:
    Client = None  # twilio is optional during development


# Configure basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# Define the RSS feed URLs for each news source.  These were tested on
# 2025‑08‑14 and were publicly accessible at the time of writing【920489464988373†L0-L33】【920489464988373†L34-L60】.
RSS_FEEDS = {
    "Folha": "http://feeds.folha.uol.com.br/mundo/rss091.xml",  # Mundo
    "Estadao": "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/internacional/",  # Internacional
    "Globo": "https://g1.globo.com/rss/g1/mundo",  # Mundo
    "NYT": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",  # World
    "WSJ": "https://feeds.a.dj.com/rss/RSSWorldNews.xml",  # World News
}


def get_rss_articles(feed_name: str, feed_url: str, max_entries: int = 5) -> List[Dict[str, str]]:
    """Parse a single RSS feed and return the most recent articles.

    Args:
        feed_name: Friendly name of the feed (used to tag the article source).
        feed_url: The URL of the RSS feed.
        max_entries: Maximum number of entries to return.

    Returns:
        A list of dictionaries containing title, link, published datetime
        (timezone aware), summary and source.
    """
    articles: List[Dict[str, str]] = []
    logging.debug(f"Fetching RSS feed for {feed_name} from {feed_url}")
    parsed = feedparser.parse(feed_url)
    for entry in parsed.entries[:max_entries]:
        title = entry.get("title", "")
        link = entry.get("link", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        published: Optional[datetime.datetime] = None
        if "published_parsed" in entry and entry.published_parsed:
            published = datetime.datetime.fromtimestamp(time.mktime(entry.published_parsed), pytz.utc)
        articles.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
                "source": feed_name,
            }
        )
    return articles


def scrape_valor_headlines(base_url: str = "https://valorinternational.globo.com", max_articles: int = 5) -> List[Dict[str, str]]:
    """Scrape the top headlines from Valor International's home page.

    Valor International does not expose a public RSS feed.  This function
    performs a simple HTML scrape of the front page to extract headline
    titles and links.  Because the site is updated frequently, you should
    adjust ``max_articles`` to balance coverage and message length.

    Args:
        base_url: URL of Valor International homepage.
        max_articles: Number of articles to retrieve.

    Returns:
        A list of dictionaries with title, link, summary (empty), published (None) and source.
    """
    logging.debug(f"Scraping Valor International headlines from {base_url}")
    try:
        response = requests.get(base_url, timeout=10)
        response.raise_for_status()
    except Exception as e:
        logging.error(f"Failed to fetch Valor International homepage: {e}")
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    headlines: List[Dict[str, str]] = []
    seen_titles: set[str] = set()
    for tag in soup.find_all(['h2', 'h3', 'a']):
        text = tag.get_text(strip=True)
        href = tag.get('href')
        if not text or not href:
            continue
        if len(text.split()) < 3:
            continue
        if text in seen_titles:
            continue
        seen_titles.add(text)
        if href.startswith('/'):
            link = base_url.rstrip('/') + href
        elif href.startswith('http'):
            link = href
        else:
            continue
        headlines.append(
            {
                "title": text,
                "link": link,
                "summary": "",
                "published": None,
                "source": "Valor",
            }
        )
        if len(headlines) >= max_articles:
            break
    return headlines


def filter_today_articles(articles: List[Dict[str, str]], tz: str = "America/Sao_Paulo") -> List[Dict[str, str]]:
    """Filter a list of articles to include only those published today.

    Args:
        articles: List of article dictionaries with a timezone‑aware 'published'.
        tz: IANA timezone string for the user's local time.

    Returns:
        Filtered list containing only articles with a published date equal to
        today's date in the specified timezone.  Articles without a date
        (e.g., scraped from Valor) are included by default.
    """
    local_tz = pytz.timezone(tz)
    today = datetime.datetime.now(local_tz).date()
    filtered: List[Dict[str, str]] = []
    for art in articles:
        published = art.get("published")
        if published is None:
            filtered.append(art)
            continue
        local_published = published.astimezone(local_tz)
        if local_published.date() == today:
            filtered.append(art)
    return filtered


def summarise_with_chatgpt(articles: List[Dict[str, str]], language: str = "pt") -> str:
    """Use the OpenAI Chat Completion API to produce a concise summary of articles.

    Each article is summarised in one to three sentences.  The summary is
    returned as a single string with clear separation between articles.  The
    output language defaults to Portuguese (``pt``) to suit Brazilian users.

    Args:
        articles: List of article dictionaries with 'title', 'link' and
            optionally 'summary'.
        language: Target language code (``pt`` for Portuguese).

    Returns:
        A string containing the combined summary.
    """
    if openai is None:
        raise RuntimeError("openai module not installed. Run 'pip install openai'.")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("Missing OPENAI_API_KEY environment variable.")
    openai.api_key = api_key
    messages = []
    system_prompt = (
        "Você é um assistente que resume notícias de forma concisa em português. "
        "Para cada artigo fornecido, produza um resumo curto (1–3 frases) "
        "destacando os fatos e eventos mais importantes. Não inclua opiniões."
    )
    messages.append({"role": "system", "content": system_prompt})
    user_content_lines = []
    for i, art in enumerate(articles, start=1):
        line = f"{i}. Título: {art['title']}\n"
        if art.get("summary"):
            summary_text = BeautifulSoup(art['summary'], "html.parser").get_text()
            line += f"Resumo: {summary_text}\n"
        line += f"Link: {art['link']}"
        user_content_lines.append(line)
    user_content = "\n\n".join(user_content_lines)
    messages.append({"role": "user", "content": user_content})
    logging.debug("Sending summarisation request to OpenAI")
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=messages,
        temperature=0.3,
        max_tokens=800,
    )
    summary = response.choices[0].message["content"].strip()
    return summary


def send_whatsapp_message(message: str) -> None:
    """Send a WhatsApp message using Twilio's API.

    This function reads Twilio credentials from environment variables and
    dispatches a single text message.  Messages are sent via the Twilio
    Sandbox or a verified WhatsApp business number.

    Args:
        message: The text to send.

    Raises:
        EnvironmentError: If required environment variables are missing.
    """
    if Client is None:
        raise RuntimeError("twilio module not installed. Run 'pip install twilio'.")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number = os.getenv("TWILIO_TO_NUMBER")
    if not all([account_sid, auth_token, from_number, to_number]):
        raise EnvironmentError("Missing one or more Twilio environment variables.")
    client = Client(account_sid, auth_token)
    logging.info("Sending WhatsApp message...")
    client.messages.create(
        body=message,
        from_=from_number,
        to=to_number,
    )
    logging.info("WhatsApp message sent successfully.")


def collect_today_news() -> List[Dict[str, str]]:
    """Aggregate today's articles from all configured feeds and Valor.

    Returns:
        A list of article dictionaries filtered for the current date.
    """
    all_articles: List[Dict[str, str]] = []
    for feed_name, feed_url in RSS_FEEDS.items():
        try:
            articles = get_rss_articles(feed_name, feed_url)
            all_articles.extend(articles)
        except Exception as exc:
            logging.error(f"Error fetching feed {feed_name}: {exc}")
    valor_articles = scrape_valor_headlines()
    all_articles.extend(valor_articles)
    filtered = filter_today_articles(all_articles)
    filtered.sort(key=lambda x: x.get("source", ""))
    return filtered


def daily_job(send_message: bool = False) -> None:
    """Collect, summarise and optionally send today's news.

    Args:
        send_message: If True, send the WhatsApp message via Twilio.  If False,
            only log the message (useful for development/testing).
    """
    logging.info("Starting daily news aggregation job")
    articles = collect_today_news()
    if not articles:
        logging.warning("No articles found for today. Nothing to summarise.")
        return
    try:
        summary = summarise_with_chatgpt(articles)
    except Exception as exc:
        logging.error(f"Failed to summarise articles: {exc}")
        return
    local_tz = pytz.timezone("America/Sao_Paulo")
    today_str = datetime.datetime.now(local_tz).strftime("%d/%m/%Y")
    message = f"Resumo das principais notícias de {today_str}:\n\n{summary}"
    logging.info("News summary prepared.")
    if send_message:
        try:
            send_whatsapp_message(message)
        except Exception as exc:
            logging.error(f"Failed to send WhatsApp message: {exc}")
    else:
        logging.info("Generated message:\n" + message)


def schedule_daily_news(send_message: bool = False) -> None:
    """Schedule the news job to run every day at 06:00 São Paulo time.

    Args:
        send_message: Whether to dispatch the message via WhatsApp.
    """
    def job_wrapper():
        daily_job(send_message=send_message)
    schedule.every().day.at("06:00").do(job_wrapper)
    logging.info("Scheduled daily job at 06:00 America/Sao_Paulo.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    daily_job(send_message=True)
    # To schedule automatically, uncomment the following line:
    # schedule_daily_news(send_message=True)
