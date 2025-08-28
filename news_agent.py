"""
news_agent.py

This module implements a news aggregation and WhatsApp delivery agent.  Every
morning at 06:00 São Paulo time the agent fetches the latest economy and
politics stories from a set of Brazilian and international publications.  The
supported sources are Folha de S.Paulo, Estadão, O Globo/G1, Valor (via
scraping), The New York Times and The Wall Street Journal.  For each source
there are dedicated RSS feeds for the economy (economia/business) and
politics (política/politics) sections, compiled in August 2025.  Valor
Econômico does not expose an RSS feed so the agent scrapes headlines from
its home page.  The collected articles are summarised into concise
Portuguese‑language sentences using OpenAI’s chat completions API and then
delivered via WhatsApp through Twilio.

To run this script you need to install the following packages:

```
pip install feedparser requests beautifulsoup4 schedule pytz twilio openai
```

Before running, set the following environment variables in your shell or in a
.env file loaded by python‑dotenv (if you choose to use it):

* `OPENAI_API_KEY` – your OpenAI API key for calling the chat completion endpoint.
* `TWILIO_ACCOUNT_SID` – your Twilio account SID.
* `TWILIO_AUTH_TOKEN` – your Twilio auth token.
* `TWILIO_FROM_NUMBER` – the WhatsApp sending number (e.g. `whatsapp:+14155238886`).
* `TWILIO_TO_NUMBER` – the destination WhatsApp number that should receive the summary (e.g. `whatsapp:+5511999999999`).

Because running scheduled jobs in long‑running processes can be brittle, you
should consider configuring a system cron job to invoke this script at
06:00 every day.  For example, on a Linux system you might add the following
line to your crontab (edit via `crontab -e`):

```
0 6 * * * /usr/bin/python3 /path/to/news_agent.py >> /var/log/news_agent.log 2>&1
```

Note: This script only prepares the messages when executed by ChatGPT during
analysis; it will not actually call external APIs.  To activate the WhatsApp
integration you must supply valid Twilio credentials and set
``send_message=True`` when calling the script.  When running in production
the environment variables above must be defined and the account must have
enough quota on both OpenAI and Twilio services.
"""

import os
import datetime
import time
import logging
import json
from typing import List, Dict, Optional, Tuple

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


# Define the RSS feed URLs for economy and politics sections of each news source.
# These URLs were compiled in August 2025; some feeds may occasionally be unavailable
# but the agent will skip any that fail to parse.  The keys combine the
# publication name and topic for easier sorting later.
RSS_FEEDS: dict[str, str] = {
    # Estadão – economia e política
    "Estadao_Economia": "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/economia/",
    "Estadao_Politica": "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/politica/",
    # Valor Econômico – usaremos scraping em vez de RSS (see scrape_valor_headlines)
    # Folha de S.Paulo – mercado (economia) e poder (política)
    "Folha_Economia": "http://feeds.folha.uol.com.br/mercado/rss091.xml",
    "Folha_Politica": "http://feeds.folha.uol.com.br/poder/rss091.xml",
    # O Globo/G1 – economia e política
    "Globo_Economia": "https://g1.globo.com/rss/g1/economia",
    "Globo_Politica": "https://g1.globo.com/rss/g1/politica",
    # The New York Times – business (economy) e politics
    "NYT_Economia": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "NYT_Politica": "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    # Wall Street Journal – business and politics.  Note: WSJ feeds may be
    # subject to access restrictions; the agent will handle errors.
    "WSJ_Economia": "https://feeds.a.dj.com/rss/RSSBusinessNews.xml",
    "WSJ_Politica": "https://feeds.a.dj.com/rss/RSSPoliticsAndPolicy.xml",
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
        # Include the source (feed name) to help the model understand the topic and
        # provenance of each article.  Many feeds encode the topic in the key
        # name (e.g. "Estadao_Economia"), so exposing it may assist the
        # summarisation in distinguishing economy and politics stories.
        line = f"{i}. Título: {art['title']}\n"
        if art.get("summary"):
            summary_text = BeautifulSoup(art['summary'], "html.parser").get_text()
            line += f"Resumo: {summary_text}\n"
        line += f"Fonte: {art.get('source', '')}\n"
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


def prepare_news_message(
    articles: List[Dict[str, str]],
    tz: str = "America/Sao_Paulo",
    max_articles: int = 10,
    max_chars: int = 1500,
) -> str:
    """Build a detailed WhatsApp message with a small summary for each article.

    This helper does not rely on the OpenAI API.  Instead it uses the summary
    provided by the RSS feed (if any) and truncates it to a few hundred
    characters.  If no summary is available, only the title, source and link
    are included.  Articles are enumerated to improve readability.

    Args:
        articles: List of article dictionaries with keys 'title', 'summary',
            'link' and 'source'.
        tz: IANA timezone string for date formatting.

    Returns:
        A string suitable for sending via WhatsApp.
    """
    # Determine the current date in the requested timezone.
    local_tz = pytz.timezone(tz)
    today_str = datetime.datetime.now(local_tz).strftime("%d/%m/%Y")
    lines: List[str] = []
    # Header for the message
    header = f"Principais notícias de {today_str} (Economia & Política):"
    lines.append(header)
    # Keep track of the message length to avoid exceeding Twilio's WhatsApp limits.
    current_length = len(header) + 2  # account for newline separators
    count = 0
    for idx, art in enumerate(articles, start=1):
        if count >= max_articles:
            break
        title = art.get("title", "").strip()
        summary_html = art.get("summary", "") or ""
        # Strip HTML tags from RSS summaries
        summary_text = BeautifulSoup(summary_html, "html.parser").get_text().strip()
        # Truncate summaries aggressively to around 150 characters
        truncated = ""
        if summary_text:
            # Remove excess whitespace
            truncated = ' '.join(summary_text.split())
            # Try to cut at the first sentence or to 150 chars
            sentences = truncated.split('.')
            if sentences:
                first_sentence = sentences[0].strip()
                if len(first_sentence) <= 150:
                    truncated = first_sentence
                else:
                    truncated = first_sentence[:150].rsplit(' ', 1)[0] + '…'
            else:
                if len(truncated) > 150:
                    truncated = truncated[:150].rsplit(' ', 1)[0] + '…'
        source = art.get("source", "")
        link = art.get("link", "")
        # Construct the message line
        line = f"{idx}. {title}"
        if truncated:
            line += f" – {truncated}"
        if source:
            line += f" (Fonte: {source})"
        if link:
            line += f"\nLink: {link}"
        # Check if adding this line would exceed the character limit
        tentative_length = current_length + len(line) + 2  # plus separators
        if tentative_length > max_chars:
            # Stop adding more articles if the limit would be exceeded
            break
        lines.append(line)
        current_length = tentative_length
        count += 1
    return "\n\n".join(lines)


def categorize_articles(articles: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Split the list of articles into separate categories based on their source key.

    The source strings in RSS_FEEDS are formatted as ``<Publication>_<Topic>`` where
    the topic is either ``Economia`` (for economy/business) or ``Politica`` (for
    politics).  This helper inspects the suffix of the source key to group
    articles accordingly.  Any article whose source is missing or does not
    explicitly end with a recognised category is placed into the ``Outros`` group.

    Args:
        articles: List of article dictionaries.

    Returns:
        A mapping of category names to lists of articles.  Keys will include
        ``Economia``, ``Politica`` and possibly ``Outros``.
    """
    categories: Dict[str, List[Dict[str, str]]] = {
        "Economia": [],
        "Politica": [],
        "Outros": [],
    }
    for art in articles:
        source = art.get("source", "")
        # Determine the topic from the portion after the last underscore
        if source and "_" in source:
            topic_key = source.split("_")[-1].lower()
            if topic_key in ("economia", "business"):
                categories["Economia"].append(art)
                continue
            if topic_key in ("politica", "politics", "policy"):
                categories["Politica"].append(art)
                continue
        # Anything else goes into 'Outros'
        categories["Outros"].append(art)
    return categories


def build_headline_message(
    category: str,
    articles: List[Dict[str, str]],
    tz: str = "America/Sao_Paulo",
    max_articles: int = 5,
) -> str:
    """Construct a brief WhatsApp message listing only the main headlines for a category.

    This helper takes a category name (e.g. ``"Politica"`` or ``"Economia"``) and a
    list of article dictionaries, then selects up to ``max_articles`` of the
    most recent items and composes a short message containing the titles and
    optional links.  Links are included to allow the recipient to read more,
    but can be removed if message length becomes an issue.

    Args:
        category: Name of the category for the message header.
        articles: List of article dictionaries to include.
        tz: IANA timezone string for date formatting.
        max_articles: Maximum number of headlines to include.

    Returns:
        A string containing the category header followed by enumerated headlines.
    """
    local_tz = pytz.timezone(tz)
    today_str = datetime.datetime.now(local_tz).strftime("%d/%m/%Y")
    header = f"Principais manchetes de {category} em {today_str}:"
    lines = [header]
    # Take only the first ``max_articles`` headlines
    for idx, art in enumerate(articles[:max_articles], start=1):
        title = art.get("title", "").strip()
        source = art.get("source", "")
        link = art.get("link", "")
        # Build the headline line; include source for context
        line = f"{idx}. {title}"
        if source:
            line += f" (Fonte: {source})"
        if link:
            line += f"\nLink: {link}"
        lines.append(line)
    return "\n\n".join(lines)


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


def send_template_message(
    content_sid: str,
    variables: Dict[str, str],
    
    ) -> None:
    """Send a WhatsApp template message using Twilio's Content API.

    This helper reads Twilio credentials from environment variables and
    dispatches a template (HSM) message.  The template must be pre‑approved
    on your WhatsApp Business Account.  The ``content_sid`` identifies the
    template and ``variables`` supplies the placeholder values (e.g. date,
    category, summary).

    Args:
        content_sid: The Content SID of the approved template.
        variables: Mapping of placeholder numbers (as strings) to values.  For
            example: {"1": "27/08/2025", "2": "Política", "3": "Manchete 1; Manchete 2"}.

    Raises:
        EnvironmentError: If required environment variables are missing.
    """
    if Client is None:
        raise RuntimeError("twilio module not installed. Run 'pip install twilio'.")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number = os.getenv("TWILIO_TO_NUMBER")
    if not all([account_sid, auth_token, from_number, to_number, content_sid]):
        raise EnvironmentError("Missing one or more Twilio environment variables or content SID.")
    client = Client(account_sid, auth_token)
    logging.info("Sending WhatsApp template message...")
    # Twilio's Content API expects content variables to be JSON-encoded.
    client.messages.create(
        from_=from_number,
        to=to_number,
        content_sid=content_sid,
        content_variables=json.dumps(variables),
    )
    logging.info("WhatsApp template message sent successfully.")


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
    # Break the articles into thematic categories and prepare two short messages,
    # one for Política and one for Economia.  Each message includes only the
    # main headlines (no summaries) to stay well below Twilio's character limit.
    categories = categorize_articles(articles)
    # Build a list of (topic, message) tuples instead of plain strings to retain context.
    messages: List[Tuple[str, str]] = []
    for topic in ("Politica", "Economia"):
        topic_articles = categories.get(topic, [])
        if not topic_articles:
            continue
        try:
            msg = build_headline_message(
                category=topic,
                articles=topic_articles,
                tz="America/Sao_Paulo",
                max_articles=5,
            )
        except Exception as exc:
            logging.error(f"Failed to build headlines message for {topic}: {exc}")
            continue
        messages.append((topic, msg))
    if not messages:
        logging.warning("No headlines messages to send after categorisation.")
        return
    logging.info(f"Prepared {len(messages)} headline message(s) for dispatch.")
    if send_message:
        # Determine if a template SID is available; if so, send via template.
        content_sid = os.getenv("CONTENT_SID_DAILY")
        # Compute today's date string for placeholder {1}
        local_tz = pytz.timezone("America/Sao_Paulo")
        today_str = datetime.datetime.now(local_tz).strftime("%d/%m/%Y")
        for topic, msg in messages:
            try:
                if content_sid:
                    # Build variables: {1}=date, {2}=topic, {3}=message content
                    variables = {"1": today_str, "2": topic, "3": msg}
                    send_template_message(content_sid, variables)
                else:
                    # Fall back to freeform message (only works within a 24h session)
                    send_whatsapp_message(msg)
                time.sleep(1)
            except Exception as exc:
                logging.error(f"Failed to send WhatsApp message: {exc}")
    else:
        # Preview the prepared messages in the logs
        for idx, (topic, msg) in enumerate(messages, start=1):
            logging.info(f"Headline message {idx} for {topic} preview:\n{msg}\n")


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
    daily_job(send_message=False)
    # To schedule automatically, uncomment the following line:
    # schedule_daily_news(send_message=True)