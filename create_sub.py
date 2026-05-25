import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
]

def main():
    # OAuth авторизация
    flow = InstalledAppFlow.from_client_secrets_file(
        "oauth-credentials.json", SCOPES
    )
    creds = flow.run_local_server(port=8080)
    
    # Сохраняем токен
    with open("token.json", "w") as f:
        f.write(creds.to_json())
    print("Токен сохранён!")
    
    # Получаем список пространств
    chat = build("chat", "v1", credentials=creds)
    spaces = chat.spaces().list().execute()
    
    print("\nДоступные пространства:")
    for space in spaces.get("spaces", []):
        print(f"  {space['name']} — {space.get('displayName', 'DM')}")

if __name__ == "__main__":
    main()