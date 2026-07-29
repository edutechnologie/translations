from google import genai
from dotenv import load_dotenv
import os
# generating text responses
# https://ai.google.dev/gemini-api/docs/text-generation
# available models found under
# https://ai.google.dev/gemini-api/docs/models

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash"
stream = client.interactions.create(
    model=MODEL,
    input="Say 'Hello, world!' in a fun way.",
    stream=True
)
print("Streaming response...")
for event in stream:
    if event.event_type == "step.delta":
        if event.delta.type == "text":
            print(event.delta.text, end="")

print("\n\n--- Done! ---")
