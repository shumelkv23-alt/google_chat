import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
]

SPACE_NAME = "spaces/AAQAmmGOtCo"
PUBSUB_TOPIC = "projects/vacanciesbot-496815/topics/chat-events-topic"

def main():
    # Загружаем сохранённый токен
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    
    # Создаём подписку на все сообщения пространства
    svc = build("workspaceevents", "v1", credentials=creds)
    
    body = {
        "targetResource": f"//chat.googleapis.com/{SPACE_NAME}",
        "eventTypes": [
            "google.workspace.chat.message.v1.created",
            "google.workspace.chat.message.v1.updated",
            "google.workspace.chat.message.v1.deleted",
        ],
        "notificationEndpoint": {
            "pubsubTopic": PUBSUB_TOPIC
        },
        "payloadOptions": {
            "includeResource": True
        }
    }
    
    print("Создаём подписку...")
    result = svc.subscriptions().create(body=body).execute()
    print("Подписка создана!")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()