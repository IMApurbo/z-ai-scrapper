this will use the Playwright to intract with zai , feed the input to ai from terminal and grab the response  from browser to terminal , and use lifetime without any issue in any project


---

# Zai Server Backend Server API Documentation

**Version:** 1.0.0  
**Description:** Local REST API bridge between the IMA-Agent VS Code extension and [chat.z.ai](https://chat.z.ai).

---

## Overview

This backend wraps a Playwright-controlled browser instance that interacts with **chat.z.ai**. It exposes a simple REST API so the VS Code extension can send messages, start new chats, and monitor the scraper.

### Architecture Highlights

- **Single Browser Thread**: Playwright’s synchronous API is not thread-safe. All browser operations run on a dedicated daemon thread (`browser-thread`).
- **FastAPI + Threading Queue**: API endpoints run on FastAPI worker threads and dispatch tasks to the browser thread via a queue + `threading.Event`.
- **Headless Mode**: Currently runs in **headed** mode (`headless=False`) to allow manual login and CAPTCHA solving.

---

## Base URL

```
http://localhost:8765
```

---

## Endpoints

### 1. `GET /status`

**Description:** Health check and browser status.

**Response:**
```json
{
  "online": true,
  "url": "https://chat.z.ai/",
  "inputReady": true,
  "responseCount": 5
}
```

Or when not ready:
```json
{
  "online": false,
  "message": "Scraper not initialized"
}
```

---

### 2. `POST /chat`

**Description:** Send a message to chat.z.ai and get the response.

**Request Body:**
```json
{
  "message": "Explain quantum entanglement in simple terms"
}
```

**Success Response (200):**
```json
{
  "response": "Quantum entanglement is a phenomenon where two or more particles...",
  "thinking": "First, I need to recall what quantum mechanics says about...",
  "timestamp": "2026-04-29T10:15:32.456789"
}
```

**Error Responses:**
- `400` — Empty message
- `500` — Browser or scraping error

---

### 3. `POST /new_chat`

**Description:** Start a new conversation (clears current chat).

**Request Body:** None

**Response:**
```json
{
  "success": true
}
```

---

### 4. `GET /history`

**Description:** Returns the full conversation history from the current chat (assistant messages only).

**Response:**
```json
{
  "history": [
    {
      "role": "assistant",
      "content": "Hello! How can I help you today?"
    },
    {
      "role": "assistant",
      "content": "Quantum entanglement occurs when..."
    }
  ]
}
```

---

### 5. `POST /refresh`

**Description:** Reloads the chat.z.ai page. Useful for recovering from broken states.

**Request Body:** None

**Response:**
```json
{
  "success": true
}
```

---

## Models

### ChatRequest
```ts
{
  message: string
}
```

### ChatResponse
```ts
{
  response: string
  thinking?: string
  timestamp?: string
  error?: string
}
```

---

## Usage Examples

### Using cURL

```bash
# Send a message
curl -X POST http://localhost:8765/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the capital of Bangladesh?"}'

# New chat
curl -X POST http://localhost:8765/new_chat

# Check status
curl http://localhost:8765/status
```

### Using Python (requests)

```python
import requests

BASE_URL = "http://localhost:8765"

response = requests.post(
    f"{BASE_URL}/chat",
    json={"message": "Hello, how are you?"}
)

print(response.json())
```

---

## CORS

CORS is enabled for **all origins** (`allow_origins=["*"]`), making it easy for the VS Code extension (running on a different origin) to communicate with the backend.

---

## Startup & Initialization

When you run the server:

1. A background thread launches Playwright + Chromium.
2. It navigates to `https://chat.z.ai/`.
3. You may need to **manually log in** and solve CAPTCHA in the opened browser window.
4. Once ready, the API becomes available at `http://localhost:8765`.

---

## Notes & Limitations

- The browser runs in **headed mode** (`headless=False`) for reliability and manual intervention (login, CAPTCHA).
- All browser operations are serialized through a single thread — no concurrent browser actions.
- Long responses may take up to ~3 minutes (timeout = 180s).
- The scraper tries to be robust against UI changes but may break if chat.z.ai significantly updates their frontend.

---

## Development

**Run the server:**
```bash
python main.py
```

**API Documentation (Interactive):**
Once running, visit:
- Swagger UI: http://localhost:8765/docs
- ReDoc: http://localhost:8765/redoc

---
 
**Purpose:** Bridge between VS Code Extension and chat.z.ai
