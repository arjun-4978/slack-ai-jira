import json
import logging
import requests
import time
import boto3
from pinecone import Pinecone

# === CONFIG ===
PINECONE_API_KEY = ""
PINECONE_INDEX = "jira-ticket-embeddings"
NAMESPACE = "ns1"
REGION = "ap-south-1"
SCORE_THRESHOLD = 0.50
#SLACK_TOKEN = ""
SLACK_TOKEN = ""
SLACK_API_URL = "https://slack.com/api/chat.postMessage"
JIRA_DOMAIN = "capillarytech.atlassian.net"
BASE_URL = f"https://{JIRA_DOMAIN}/rest/api/3"
AUTH_HEADER = {
    "Authorization": "Basic ",  # Replace with your actual Basic Auth",
    "Accept": "application/json"
}
MODEL_ID = "anthropic.claude-v2"
MAX_TOKENS = 1024
TOP_K_MATCHES = 5
MAX_RETRIES = 3

# === Setup ===
logger = logging.getLogger()
logger.setLevel(logging.INFO)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
BEDROCK_CLAUDE = boto3.client("bedrock-runtime", region_name="us-east-1")
pc = Pinecone(api_key=PINECONE_API_KEY)
index = pc.Index(PINECONE_INDEX)

# === JIRA Fetch ===
def fetch_summary_and_description(issue_key):
    url = f"{BASE_URL}/issue/{issue_key}?fields=summary"
    res = requests.get(url, headers=AUTH_HEADER)
    if res.status_code != 200:
        logger.error(f"Failed to fetch issue {issue_key}: {res.status_code} {res.text}")
        return ""
    data = res.json()
    return data.get("fields", {}).get("summary", "")

def fetch_latest_comments(issue_key):
    url = f"{BASE_URL}/issue/{issue_key}/comment?orderBy=-created&maxResults=30"
    res = requests.get(url, headers=AUTH_HEADER)
    if res.status_code != 200:
        return []
    comments = res.json().get("comments", [])
    all_comments = []
    for idx, c in enumerate(comments, 1):
        text = [p.get("text", "") for b in c.get("body", {}).get("content", []) if b["type"] == "paragraph" for p in b.get("content", []) if p["type"] == "text"]
        if text:
            all_comments.append(f"Comment-{idx:02d}: {' '.join(text)}")
    return all_comments

# === Claude Summarization ===
def build_prompt(key, summary, comments):
    text = f"JIRA Key: {key}\n\nSummary:\n- {summary}\n\nLatest Comments:\n"
    text += "\n".join(comments)
    return text

def summarize_with_claude(prompt_text):
    logger.info(f"Prompt to Claude:\n{prompt_text}")
    body = json.dumps({
        "prompt": f"\n\nHuman: Please summarize the following JIRA issue in bullet points:\n\n{prompt_text}\n\nAssistant:",
        "max_tokens_to_sample": MAX_TOKENS,
        "temperature": 0.5,
        "top_k": 250,
        "top_p": 1.0,
        "stop_sequences": ["\n\nHuman:"]
    })
    response = BEDROCK_CLAUDE.invoke_model(
        modelId=MODEL_ID,
        body=body,
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response["body"].read())
    return result.get("completion", "<No summary returned>")

# === Titan Embedding ===
def get_query_embedding(text):
    body = json.dumps({"inputText": text})
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        contentType="application/json",
        accept="application/json",
        body=body
    )
    result = json.loads(response["body"].read())
    return result["embedding"]

# === Pinecone Semantic Search ===
def search_pinecone(query, top_k=TOP_K_MATCHES):
    embedding = get_query_embedding(query)
    response = index.query(
        namespace=NAMESPACE,
        vector=embedding,
        top_k=top_k,
        include_metadata=True
    )
    matches = response.get("matches", [])
    return [m for m in matches if m["score"] >= SCORE_THRESHOLD]

# === Slack Posting with Retry ===
def send_slack_message_with_retry(channel, thread_ts, blocks):
    headers = {
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "channel": channel,
        "thread_ts": thread_ts,
        "blocks": blocks
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"Slack POST attempt {attempt}")
            resp = requests.post(SLACK_API_URL, headers=headers, json=payload)
            logger.info(f"Slack response: {resp.status_code} - {resp.text}")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return data
                elif data.get("error") == "ratelimited":
                    wait_time = 2 ** attempt
                    logger.warning(f"Rate limited. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Slack error: {data.get('error')}")
                    break
            else:
                logger.warning("Non-200 Slack response. Retrying...")
                time.sleep(2)
        except Exception as e:
            logger.error(f"Slack failure on attempt {attempt}: {str(e)}")
            time.sleep(2)
    logger.error("Slack message failed after retries.")
    return None

# === Lambda Entry ===
def lambda_handler(event, context):
    try:
        logger.info("Event: %s", json.dumps(event))
        channel = event.get("channel")
        thread_ts = event.get("thread_ts")
        text = event.get("text", "")

        logger.info(f"Searching Pinecone with query: {text}")
        matches = search_pinecone(text)
        logger.info(f"Found {len(matches)} matches")

        if not matches:
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": ":mag: *No similar JIRA tickets found.*"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": ":white_check_mark: You're all set to proceed!"}}
            ]
            send_slack_message_with_retry(channel, thread_ts, blocks)
            return {"statusCode": 200, "body": json.dumps("No matches found")}

        matches.sort(key=lambda x: x["score"], reverse=True)
        matches = matches[:TOP_K_MATCHES]
        logger.info(f"Sorted top {TOP_K_MATCHES} matches")

        for idx, match in enumerate(matches, 1):
            raw_key = match["metadata"].get("key", "")
            logger.info(f"[{idx}] Processing raw key: {raw_key}")

            if "/browse/" in raw_key:
                issue_key = raw_key.split("/browse/")[-1].strip("/")
                issue_url = raw_key
            else:
                issue_key = raw_key
                issue_url = f"https://{JIRA_DOMAIN}/browse/{issue_key}"

            summary = fetch_summary_and_description(issue_key)
            if not summary:
                logger.warning(f"No summary for issue {issue_key}, skipping.")
                continue

            comments = fetch_latest_comments(issue_key)
            prompt = build_prompt(issue_key, summary, comments)
            ticket_summary = summarize_with_claude(prompt)

            trophy = ":trophy: " if idx == 1 else ""
            blocks = [
                {"type": "header", "text": {"type": "plain_text", "text": f"{trophy}Match {idx}: {issue_key}", "emoji": True}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Summary:*\n{summary}"},
                    {"type": "mrkdwn", "text": f"*Score:*\n{match['score']:.4f}"},
                    {"type": "mrkdwn", "text": f"*Status:*\n{match['metadata'].get('status')}"},
                    {"type": "mrkdwn", "text": f"*Priority:*\n{match['metadata'].get('priority')}"}
                ]},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Link:* <{issue_url}|{issue_key}>"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary from Claude:*\n```{ticket_summary.strip()}```"}},
                {"type": "divider"}
            ]

            send_slack_message_with_retry(channel, thread_ts, blocks)
            time.sleep(1)

        return {"statusCode": 200, "body": json.dumps("Posted top matches to Slack")}

    except Exception as e:
        logger.exception("Error in Lambda")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
