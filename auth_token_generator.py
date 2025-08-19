import requests
import base64

CLIENT_ID = 'B58AE9297BD94ECE986E985D58A9C198'
CLIENT_SECRET = 'yWrGGca362CQI4mNArORumhNnJ9839mclowqiMJeKySYTj6Y'

# Use client credentials grant for machine-to-machine authentication
token_data = {
    'grant_type': 'client_credentials',
    'scope': 'accounting.transactions accounting.contacts accounting.settings accounting.reports.read'
}

# Create basic auth header
credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
encoded_credentials = base64.b64encode(credentials.encode()).decode()

headers = {
    'Content-Type': 'application/x-www-form-urlencoded',
    'Authorization': f'Basic {encoded_credentials}'
}

try:
    print('Requesting bearer token...')
    response = requests.post('https://identity.xero.com/connect/token', 
                           data=token_data, headers=headers)
    
    if response.status_code == 200:
        token_response = response.json()
        if 'access_token' in token_response:
            print(f'Access Token: {token_response["access_token"]}')
            print(f'Expires in: {token_response["expires_in"]} seconds')
        else:
            print('Error:', token_response)
    else:
        print(f'HTTP Error {response.status_code}: {response.text}')
        
except Exception as e:
    print(f'Error: {e}')