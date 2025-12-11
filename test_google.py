import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
gc = gspread.authorize(creds)

sheet = gc.open("LitCafe_Control").sheet1

data = sheet.get_all_records()

print("Таблица прочитана успешно!")
print(data)
