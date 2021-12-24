'''
This script creates a new folder owned by the service account in SERVICE_ACCOUNT_CREDENTIALS
and shares it with HUMAN_ACCOUNT_EMAIL. This allows the service account to create and retrieve
sheets within that folder that it can edit programatically. (If you try to do so vise versa, as
in have HUMAN_ACCOUNT_EMAIL manually create a folder that it shares with the service account,
the service account is unable to edit sheets within that folder programatically via the sheets api.)

This is a one-off script that shouldn't need to be run often, but is useful to document for future projects.
'''
import os
from googleapiclient.discovery import build

SERVICE_ACCOUNT_CREDENTIALS = 'real-estate-investing-335904-05fe8a22753f.json'
HUMAN_ACCOUNT_EMAIL = 'ibeckermayer@gmail.com'


def main():
  os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = SERVICE_ACCOUNT_CREDENTIALS
  service = build('drive', 'v3')

  folder_metadata = {'name': 'Real Estate', 'mimeType': 'application/vnd.google-apps.folder'}
  folder = service.files().create(body=folder_metadata, fields='id').execute()
  folder_id = folder.get("id")
  print(f'REAL_ESTATE_FOLDER_ID = "{folder.get("id")}"')

  permission_metadata = {'role': 'writer', 'type': 'user', 'emailAddress': HUMAN_ACCOUNT_EMAIL}
  permission = service.permissions().create(fileId=folder_id, fields='*',
                                            body=permission_metadata).execute()
  print(permission)


if __name__ == '__main__':
  # main()
  exit(0)
