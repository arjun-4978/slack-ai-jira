import json
import boto3
import logging
import os
import base64
import requests
from urllib.parse import parse_qs
#from slack_sdk import WebClient
import os
# Logging setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# lambda Invocations
LAMBDA_CLIENT = boto3.client("lambda")
dynamo = boto3.resource("dynamodb")



# Initialize Slack WebClient
#slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
# Slack setup
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_API_URL = os.environ.get("SLACK_API_URL")
#SLACK_API_URL = "https://slack.com/api"
SEARCH_FUNCTION_NAME = os.environ.get("SEARCH_FUNCTION_NAME")
CLAUDE_FUNCTION_NAME="jira-ticket-generation-Claude"
event_table = dynamo.Table("slack_events")


# --- Prevent duplicate event processing ---
def is_duplicate_event(event_id):
    try:
        response = event_table.get_item(Key={"event_id": event_id})
        return "Item" in response
    except Exception as e:
        logger.error(f"Error checking event_id in DynamoDB: {e}")
        return False

def mark_event_processed(event_id):
    try:
        event_table.put_item(Item={"event_id": event_id})
    except Exception as e:
        logger.error(f"Error writing event_id to DynamoDB: {e}")

def lambda_handler(event, context):
    try:
        raw_body = event.get("body", "")
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        headers = event.get("headers", {})
        content_type = headers.get("content-type", "")

        if content_type.startswith("application/x-www-form-urlencoded"):
            form_data = parse_qs(raw_body)
            payload_str = form_data.get("payload", [None])[0]
            if not payload_str:
                raise ValueError("Missing Slack payload")

            body = json.loads(payload_str)
            logger.info(f"Slack interactive payload: {json.dumps(body)}")

            if body.get("type") == "block_actions":
                action_id = body["actions"][0]["action_id"]
                if action_id == "open_ticket_modal":
                    trigger_id = body["trigger_id"]
                    channel = body["channel"]["id"]
                    thread_ts = body["container"].get("thread_ts") or body["container"].get("message_ts")
                    metadata_str = body["actions"][0].get("value", "{}")
                    metadata = json.loads(metadata_str)
                    summary_prefill = metadata.get("summary_prefill", "")
                    description_prefill = metadata.get("description_prefill", "")
                    user_message = metadata.get("user_message", "")

                    return open_modal(trigger_id, channel, thread_ts, summary_prefill, description_prefill, user_message)

            elif body.get("type") == "view_submission":
                return handle_modal_submission(body)

            return {"statusCode": 200, "body": "OK"}

        body = json.loads(raw_body)
        logger.info(f"Slack event body: {json.dumps(body)}")

        if body.get("type") == "url_verification":
            return {"statusCode": 200, "body": body.get("challenge")}

        if body.get("type") == "event_callback":
            event_data = body.get("event", {})
            event_id = body.get("event_id")
            if is_duplicate_event(event_id):
                logger.info(f"Duplicate event detected: {event_id}")
                return {"statusCode": 200, "body": "Duplicate ignored"}

            mark_event_processed(event_id)

            if event_data.get("type") == "app_mention":
                channel = event_data["channel"]
                user = event_data["user"]
                thread_ts = event_data["ts"]
                full_text = event_data.get("text", "")
                user_message = " ".join(full_text.split()[1:])
                text = f"👀 <@{user}>, hold tight! We’re checking for similar tickets..."
                payload = {
                    "channel": channel,
                    "text": text,
                    "thread_ts": thread_ts
                }

                slack_post("chat.postMessage", payload)

                # Call Search Lambda
                search_payload = {
                    "channel": channel,
                    "text": user_message,
                    "thread_ts": thread_ts
                }
                search_response = LAMBDA_CLIENT.invoke(
                    FunctionName=SEARCH_FUNCTION_NAME,
                    InvocationType="RequestResponse",
                    Payload=json.dumps(search_payload).encode("utf-8")
                )
                search_data = json.load(search_response["Payload"])

                # Call Claude Lambda
                try:
                    response = LAMBDA_CLIENT.invoke(
                        FunctionName=CLAUDE_FUNCTION_NAME,
                        InvocationType="RequestResponse",
                        Payload=json.dumps({"text": user_message}).encode("utf-8")
                    )
                    result = json.loads(response["Payload"].read())
                    body = json.loads(result.get("body", "{}"))
                    summary = body.get("summary", "")
                    description = body.get("description", "")
                except Exception as e:
                    logger.error(f"Error invoking Claude Lambda: {e}")
                    summary = user_message
                    description = user_message

                metadata = json.dumps({
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "summary_prefill": summary,
                    "description_prefill": description,
                    "user_message": user_message
                })

                payload = {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "text": f"Hi <@{user}>! Would you like to create a ticket?",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"Hi <@{user}>! Would you like to create a new ticket? We’ll use AI to generate a summary/description from your message to create ticket"
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "open_ticket_modal",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Create Jira Ticket", "emoji": True},
                                    "action_id": "open_ticket_modal",
                                    "value": metadata
                                }
                            ]
                        }
                    ]
                }
                return slack_post("chat.postMessage", payload)

        return {"statusCode": 200, "body": "OK"}

    except Exception as e:
        logger.exception("Error processing request")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

def invoke_search_lambda(channel, message, thread_ts):
    payload = {
        "channel": channel,
        "text": message,
        "thread_ts": thread_ts
    }
    response = lambda_client.invoke(
        FunctionName=SEARCH_FUNCTION_NAME,
        InvocationType='RequestResponse',
        Payload=json.dumps(payload).encode('utf-8')
    )
    response_payload = json.load(response['Payload'])
    return response_payload

def send_modal_button(channel, thread_ts, user, user_message):
    try:
        response = LAMBDA_CLIENT.invoke(
            FunctionName="jira-ticket-generation-Claude",
            InvocationType="RequestResponse",
            Payload=json.dumps({"text": user_message}).encode("utf-8")
        )
        result = json.loads(response["Payload"].read())
        body = json.loads(result.get("body", "{}"))
        summary = body.get("summary", "")
        description = body.get("description", "")
    except Exception as e:
        logger.error(f"Error invoking Claude Lambda: {e}")
        summary = user_message
        description = user_message

    metadata = json.dumps({
        "channel": channel,
        "thread_ts": thread_ts,
        "summary_prefill": summary,
        "description_prefill": description,
        "user_message": user_message
    })

    payload = {
        "channel": channel,
        "thread_ts": thread_ts,
        "text": f"Hi <@{user}>! Would you like to create a ticket?",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Hi <@{user}>! Would you like to create a new ticket? We’ll use AI to generate a summary/description from your message."
                }
            },
            {
                "type": "actions",
                "block_id": "open_ticket_modal",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Create Jira Ticket", "emoji": True},
                        "action_id": "open_ticket_modal",
                        "value": metadata
                    }
                ]
            }
        ]
    }
    return slack_post("chat.postMessage", payload)
def open_modal(trigger_id, channel, thread_ts, summary_prefill="", description_prefill="", user_message=""):
    metadata = json.dumps({"channel": channel, "thread_ts": thread_ts, "user_message": user_message})

    payload = {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": "ticket_creation_modal",
            "private_metadata": metadata,
            "title": {"type": "plain_text", "text": "Create New Ticket"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "issuetype_block",
                    "label": {"type": "plain_text", "text": "Issue Type"},
                    "element": {
                        "type": "static_select",
                        "action_id": "issuetype_input",
                        "placeholder": {"type": "plain_text", "text": "Choose issue type"},
                        "options": [
                            {"text": {"type": "plain_text", "text": t}, "value": t}
                            for t in ["Bug", "Task", "Story"]
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "summary_block",
                    "label": {"type": "plain_text", "text": "Summary"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "summary_input",
                        "multiline": True,
                        "initial_value": summary_prefill
                    }
                },
                {
                    "type": "input",
                    "block_id": "description_block",
                    "label": {"type": "plain_text", "text": "Description"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "description_input",
                        "multiline": True,
                        "initial_value": description_prefill
                    }
                },
                {
                    "type": "input",
                    "block_id": "priority_block",
                    "label": {"type": "plain_text", "text": "Priority"},
                    "element": {
                        "type": "static_select",
                        "action_id": "priority_input",
                        "placeholder": {"type": "plain_text", "text": "Choose priority"},
                        "options": [
                            {"text": {"type": "plain_text", "text": p}, "value": p}
                            for p in ["Low-P3", "Medium-P2", "High-P1", "Highest-P0"]
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "brand_block",
                    "label": {"type": "plain_text", "text": "Brand"},
                    "element": {
                        "type": "static_select",
                        "action_id": "brand_input",
                        "placeholder": {"type": "plain_text", "text": "Choose brand"},
                        "options": [
                            {"text": {"type": "plain_text", "text": b}, "value": b}
                            for b in ["Fortress", "Indigi", "Sunoco", "Aape"]
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "env_block",
                    "label": {"type": "plain_text", "text": "Environment"},
                    "element": {
                        "type": "static_select",
                        "action_id": "env_input",
                        "placeholder": {"type": "plain_text", "text": "Choose environment"},
                        "options": [
                            {"text": {"type": "plain_text", "text": e}, "value": e}
                            for e in ["Prod", "Golive", "UAT", "Demo"]
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "component_block",
                    "label": {"type": "plain_text", "text": "Component"},
                    "element": {
                        "type": "static_select",
                        "action_id": "component_input",
                        "placeholder": {"type": "plain_text", "text": "Choose component"},
                        "options": [
                            {"text": {"type": "plain_text", "text": c}, "value": c}
                            for c in ["API", "Badges", "AWS", "Engage", "CDP", "Loyalty"]
                        ]
                    }
                }
            ]
        }
    }

    return slack_post("views.open", payload)

# --- Handle Modal Submission ---
def handle_modal_submission(body):
    view = body.get("view", {})
    state = view.get("state", {}).get("values", {})
    meta = json.loads(view.get("private_metadata", "{}"))
    original_message = meta.get("user_message", "")
    channel = meta.get("channel")
    thread_ts = meta.get("thread_ts")

    summary = state["summary_block"]["summary_input"]["value"]
    description = state["description_block"]["description_input"]["value"]
    brand = state["brand_block"]["brand_input"]["selected_option"]["value"]
    env = state["env_block"]["env_input"]["selected_option"]["value"]
    issuetype = state["issuetype_block"]["issuetype_input"]["selected_option"]["value"]
    priority = state["priority_block"]["priority_input"]["selected_option"]["value"]
    component = state["component_block"]["component_input"]["selected_option"]["value"]

    try:
        # --- Create JIRA Issue ---
        jira_url = os.environ.get("JIRA_URL")
        headers = {
            "Content-Type": "application/json",
            "Authorization": os.environ.get("JIRA_AUTH_TOKEN") 
        }

        jira_payload = {
            "fields": {
                "project": {"key": "CJ"},
                "summary": summary,
                "description": description,
                "issuetype": {"name": issuetype},
                "priority": {"name": priority},
                "customfield_11997": [{"value": brand}],
                "customfield_11800": [{"value": env}],
                "components": [{"name": component}],
                "labels": [ "slack-bot-creation"]
            }
        }

        response = requests.post(jira_url, headers=headers, json=jira_payload)
        response.raise_for_status()
        issue_data = response.json()
        issue_key = issue_data["key"]
        issue_url = f"https://capillarytech.atlassian.net/browse/{issue_key}"

        # --- Slack Message ---
        message = (
            f"🎟️ *Your JIRA Ticket Has Been Created!*\n\n"
            f"*📝 Summary:* `{summary}`\n"
            f"*🧾 Description:* `{description}`\n"
            f"*🏷️ Brand:* `{brand}`\n"
            f"*🌐 Environment:* `{env}`\n"
            f"*📌 Issue Type:* `{issuetype}`\n"
            f"*⚠️ Priority:* `{priority}`\n"
            f"*🧩 Component:* `{component}`\n\n\n\n"
            f"🛠️ *Need changes or want to attach files?*\n"
            f"Click the link above to update the ticket directly.\n\n"
            f"🔗 *View Ticket:* <{issue_url}|{issue_url}>\n\n\n\n"
        )

        slack_post("chat.postMessage", {
            "channel": channel,
            "thread_ts": thread_ts,
            "text": message
        })
    except Exception as e:
        logger.error(f"Failed to create JIRA ticket: {e}")
        slack_post("chat.postMessage", {
            "channel": channel,
            "thread_ts": thread_ts,
            "text": f"❌ Failed to create the JIRA ticket. Please try again or contact support.\n_Error: {str(e)}_"
        })
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({})
    }
# --- Send POST to Slack API ---
def slack_post(endpoint, payload):
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    response = requests.post(f"{SLACK_API_URL}/{endpoint}", headers=headers, json=payload)
    logger.info(f"Slack {endpoint} response: {response.text}")
    return {
        "statusCode": response.status_code,
        "body": response.text
    }
