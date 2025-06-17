import boto3
import json
import re

MODEL_ID = "anthropic.claude-v2"
MAX_TOKENS = 1024

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

def extract_summary_and_description(text):
    """
    Extracts the summary and description from Claude's output.
    Expected format:
    Summary: ...
    Description: ...
    """
    summary = ""
    description = ""

    summary_match = re.search(r"Summary:\s*(.*)", text)
    description_match = re.search(r"Description:\s*(.*)", text, re.DOTALL)

    if summary_match:
        summary = summary_match.group(1).strip()
    if description_match:
        description = description_match.group(1).strip()

    return summary, description

def lambda_handler(event, context):
    user_input = event.get("text", "")

    if not user_input.strip():
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "No input text provided"})
        }

    prompt = f"""Human: Based on the following user input, write a clear and concise JIRA ticket summary and description. 
Avoid generic preambles. Return only the result in the format below without extra commentary.

Format:
Summary: <short summary>
Description: <detailed description>

User Input:
\"\"\"{user_input}\"\"\"

Assistant:"""

    try:
        response = bedrock.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "prompt": prompt,
                "max_tokens_to_sample": MAX_TOKENS,
                "temperature": 0.7,
                "stop_sequences": ["\n\nHuman:"]
            })
        )

        response_body = json.loads(response["body"].read())
        completion = response_body.get("completion", "")

        summary, description = extract_summary_and_description(completion)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "summary": summary,
                "description": description
            })
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }
