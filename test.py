import requests
import json

url = "https://openai.chatgpt4mena.com/v1/chat/completions"

headers = {
    "Content-Type": "application/json",
    "Authorization": "Bearer sk-oWEosj1x4FIUldlCQMdlS56OfCZ8XmvnWJInocffASHDNgbV"
}

data = {
    "model": "gpt-4.1",
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Tell me a short story about a robot."}
    ],
    "stream": True
}

response = requests.post(url, headers=headers, json=data, stream=True)

# Handle the streaming response
for line in response.iter_lines():
    if line:
        decoded_line = line.decode('utf-8')
        if decoded_line.startswith('data: '):
            json_data = decoded_line[6:]  # Remove 'data: ' prefix
            if json_data.strip() == '[DONE]':
                break
            try:
                chunk = json.loads(json_data)
                if 'choices' in chunk and len(chunk['choices']) > 0:
                    delta = chunk['choices'][0].get('delta', {})
                    if 'content' in delta:
                        print(delta['content'], end='', flush=True)
            except json.JSONDecodeError:
                continue