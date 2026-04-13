# 🤖 Agente LLaMA + Telegram Bot

Bot do Telegram que conversa com um modelo LLaMA rodando **localmente** via Ollama. Zero custo de API, 100% privado.

---

## 📋 Pré-requisitos

- Python 3.10+
- [Ollama](https://ollama.com) instalado
- Conta no Telegram e um bot criado via [@BotFather](https://t.me/BotFather)

---

## 🚀 Configuração Passo a Passo

### 1. Instale o Ollama

```bash
# Linux / macOS
curl -fsSL https://ollama.com/install.sh | sh

# Windows: baixe o instalador em https://ollama.com/download
```

### 2. Baixe um modelo LLaMA

```bash
# Recomendado para começar (leve e rápido)
ollama pull llama3.2

# Alternativas:
ollama pull llama3.1        # Mais capaz, mais pesado
ollama pull mistral          # Ótimo custo-benefício
ollama pull phi3             # Muito leve, bom para PCs modestos
```

### 3. Crie seu Bot no Telegram

1. Abra o Telegram e procure por **@BotFather**
2. Envie `/newbot` e siga as instruções
3. Copie o **token** gerado (ex: `7123456789:AAF...`)

### 4. Descubra seu ID no Telegram (recomendado para segurança)

1. Procure por **@userinfobot** no Telegram
2. Inicie uma conversa — ele mostrará seu ID numérico

### 5. Configure o projeto

```bash
# Clone ou copie os arquivos para uma pasta
cd telegram-llama-bot

# Crie o ambiente virtual
python -m venv venv
source venv/bin/activate        # Linux/macOS
# venv\Scripts\activate         # Windows

# Instale dependências
pip install -r requirements.txt

# Configure as variáveis de ambiente
cp .env.example .env
nano .env  # ou abra com seu editor favorito
```

Edite o `.env`:
```env
TELEGRAM_TOKEN=7123456789:AAF...seu_token_aqui...
OLLAMA_MODEL=llama3.2
ALLOWED_USERS=123456789   # Seu ID do Telegram
SYSTEM_PROMPT=Você é um assistente pessoal inteligente. Responda em português.
```

### 6. Inicie o Ollama

```bash
# Em um terminal separado (ou como serviço)
ollama serve
```

### 7. Rode o bot

```bash
python bot.py
```

---

## 💬 Comandos Disponíveis no Bot

| Comando | Descrição |
|---------|-----------|
| `/start` | Mensagem de boas-vindas e instruções |
| `/clear` | Limpa o histórico da conversa |
| `/modelo` | Mostra o modelo em uso |
| `/status` | Verifica se o Ollama está ativo e lista modelos |

---

## 🔄 Rodar como Serviço (deixar sempre ligado)

### Linux com systemd

Crie o arquivo `/etc/systemd/system/telegram-llama-bot.service`:

```ini
[Unit]
Description=Telegram LLaMA Bot
After=network.target

[Service]
Type=simple
User=SEU_USUARIO
WorkingDirectory=/caminho/para/telegram-llama-bot
ExecStart=/caminho/para/telegram-llama-bot/venv/bin/python bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-llama-bot
sudo systemctl start telegram-llama-bot

# Ver logs
sudo journalctl -u telegram-llama-bot -f
```

### Com PM2 (Node.js)

```bash
npm install -g pm2
pm2 start bot.py --interpreter python3 --name llama-bot
pm2 startup   # Para iniciar no boot
pm2 save
```

---

## ⚙️ Personalização

### Mudar a personalidade do agente

Edite `SYSTEM_PROMPT` no `.env`:
```env
SYSTEM_PROMPT=Você é um assistente especializado em programação Python. Sempre forneça exemplos de código. Responda em português.
```

### Usar modelo diferente

```bash
ollama pull mistral
```
```env
OLLAMA_MODEL=mistral
```

### Liberar acesso para mais pessoas

```env
ALLOWED_USERS=123456789,987654321,555000111
```

---

## 🖥️ Requisitos de Hardware

| Modelo | RAM Mínima | Recomendado |
|--------|-----------|-------------|
| phi3 (3.8B) | 4 GB | 8 GB |
| llama3.2 (3B) | 4 GB | 8 GB |
| mistral (7B) | 8 GB | 16 GB |
| llama3.1 (8B) | 8 GB | 16 GB |

> 💡 Com GPU NVIDIA, o Ollama usa automaticamente a GPU para respostas muito mais rápidas.

---

## 🔒 Segurança

- Sempre defina `ALLOWED_USERS` com seus IDs para evitar que outras pessoas usem seu bot
- Nunca compartilhe o arquivo `.env`
- O bot roda localmente — seus dados não saem da sua máquina
