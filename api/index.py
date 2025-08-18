import os
import logging
import asyncio
import html
import json
import traceback
import io
import time
import sys

from flask import Flask, request, render_template_string, session, redirect, url_for, flash, jsonify
import functools
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
import google.generativeai as genai
import PIL.Image
import pymupdf  # fitz

# Carregar variáveis de ambiente do arquivo.env (para desenvolvimento local)
load_dotenv()

# --- Configuração do Logging ---
# Remove o handler padrão para evitar duplicação de logs.
root_logger = logging.getLogger()
if root_logger.handlers:
    for handler in root_logger.handlers:
        root_logger.removeHandler(handler)

# Formato do log
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
root_logger.setLevel(logging.INFO) # Define o nível mínimo para o logger raiz

# Handler para stdout (INFO e DEBUG)
class InfoFilter(logging.Filter):
    def filter(self, record):
        return record.levelno <= logging.INFO

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.DEBUG) # Captura a partir de DEBUG
stdout_handler.addFilter(InfoFilter())
stdout_handler.setFormatter(log_formatter)
root_logger.addHandler(stdout_handler)

# Handler para stderr (WARNING, ERROR, CRITICAL)
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.WARNING) # Captura a partir de WARNING
stderr_handler.setFormatter(log_formatter)
root_logger.addHandler(stderr_handler)

logger = logging.getLogger(__name__)

# Credenciais e IDs
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DEVELOPER_CHAT_ID = os.environ.get('DEVELOPER_CHAT_ID')

# Credenciais da Interface Web de Admin
FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY')
ADMIN_USER = os.environ.get('ADMIN_USER')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')

# Armazenamento de fallback para o histórico de chat.
# ATENÇÃO: Em um ambiente serverless como a Vercel, este dicionário pode ser resetado
# a qualquer momento entre as invocações da função. Ele serve apenas como um
# fallback temporário e não garante a persistência dos dados.
temporary_chat_contexts = {}


if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("As variáveis de ambiente TELEGRAM_BOT_TOKEN e GEMINI_API_KEY são obrigatórias.")

# --- Config Management ---

DEFAULT_SAFETY_SETTINGS = {
    "HARM_CATEGORY_HARASSMENT": "BLOCK_MEDIUM_AND_ABOVE",
    "HARM_CATEGORY_HATE_SPEECH": "BLOCK_MEDIUM_AND_ABOVE",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_MEDIUM_AND_ABOVE",
    "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_MEDIUM_AND_ABOVE",
}

def get_all_configs():
    """Busca todas as configurações do Vercel Edge Config usando a API REST."""
    edge_config_url = os.environ.get('EDGE_CONFIG')
    edge_config_token = os.environ.get('VERCEL_EDGE_CONFIG_TOKEN')

    if not edge_config_url or not edge_config_token:
        logger.warning("Edge Config env vars not found. Using default configs.")
        return {
            'system_instruction': "You are a helpful assistant.",
            'safety_settings': DEFAULT_SAFETY_SETTINGS
        }

    headers = {'Authorization': f'Bearer {edge_config_token}'}

    try:
        response = requests.get(f"{edge_config_url}/items", headers=headers, params={'keys': ['system_instruction', 'safety_settings']})
        response.raise_for_status()
        configs = response.json()
        configs.setdefault('system_instruction', "You are a helpful assistant.")
        configs.setdefault('safety_settings', DEFAULT_SAFETY_SETTINGS)
        return configs
    except Exception as e:
        logger.error(f"Could not fetch from Edge Config, falling back to defaults: {e}")
        return {
            'system_instruction': "You are a helpful assistant.",
            'safety_settings': DEFAULT_SAFETY_SETTINGS
        }

def get_config_item(key: str):
    """Busca um item de configuração específico do Vercel Edge Config."""
    edge_config_url = os.environ.get('EDGE_CONFIG')
    edge_config_token = os.environ.get('VERCEL_EDGE_CONFIG_TOKEN')

    if not edge_config_url or not edge_config_token:
        logger.warning(f"Edge Config env vars not found. Cannot get item '{key}'.")
        return None

    headers = {'Authorization': f'Bearer {edge_config_token}'}

    try:
        response = requests.get(f"{edge_config_url}/item/{key}", headers=headers)
        if response.status_code == 404:
            logger.info(f"Config item '{key}' not found in Edge Config.")
            return None
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Could not fetch item '{key}' from Edge Config: {e}")
        return None

def save_config_item(key, value):
    """Salva um item de configuração no Vercel Edge Config usando a API REST."""
    edge_config_url = os.environ.get('EDGE_CONFIG')
    edge_config_token = os.environ.get('VERCEL_EDGE_CONFIG_TOKEN')

    if not edge_config_url or not edge_config_token:
        logger.error("Edge Config env vars not found. Cannot save config.")
        return False

    headers = {
        'Authorization': f'Bearer {edge_config_token}',
        'Content-Type': 'application/json'
    }
    payload = {
        "items": [
            {"operation": "update", "key": key, "value": value}
        ]
    }

    try:
        response = requests.patch(f"{edge_config_url}/items", headers=headers, json=payload)
        response.raise_for_status()
        logger.info(f"Config item '{key}' saved to Edge Config.")
        return True
    except Exception as e:
        logger.error(f"Could not save to Edge Config: {e}", exc_info=True)
        return False

def delete_config_item(key: str):
    """Deleta um item de configuração no Vercel Edge Config."""
    edge_config_url = os.environ.get('EDGE_CONFIG')
    edge_config_token = os.environ.get('VERCEL_EDGE_CONFIG_TOKEN')

    if not edge_config_url or not edge_config_token:
        logger.error("Edge Config env vars not found. Cannot delete config.")
        return False

    headers = {
        'Authorization': f'Bearer {edge_config_token}',
        'Content-Type': 'application/json'
    }
    payload = {
        "items": [
            {"operation": "delete", "key": key}
        ]
    }

    try:
        response = requests.patch(f"{edge_config_url}/items", headers=headers, json=payload)
        response.raise_for_status()
        logger.info(f"Config item '{key}' deleted from Edge Config.")
        return True
    except Exception as e:
        logger.error(f"Could not delete from Edge Config: {e}", exc_info=True)
        return False

def get_chat_context(chat_id: int) -> list:
    """
    Busca o histórico de chat.
    Tenta primeiro o Edge Config, depois o fallback em memória.
    """
    context_key = f"context_{chat_id}"
    context = get_config_item(context_key)
    if context is not None:
        logger.info(f"Contexto para o chat {chat_id} encontrado no Edge Config.")
        return context

    # Fallback para o armazenamento em memória
    if chat_id in temporary_chat_contexts:
        logger.info(f"Contexto para o chat {chat_id} encontrado no armazenamento temporário.")
        return temporary_chat_contexts[chat_id]

    logger.info(f"Nenhum contexto encontrado para o chat {chat_id}.")
    return []

def save_chat_context(chat_id: int, context: list):
    """
    Salva o histórico de chat.
    Tenta primeiro o Edge Config, se falhar, usa o fallback em memória.
    """
    context_key = f"context_{chat_id}"
    if save_config_item(context_key, context):
        logger.info(f"Contexto para o chat {chat_id} salvo no Edge Config.")
        # Se o salvamento no Edge Config for bem-sucedido, remove do fallback
        if chat_id in temporary_chat_contexts:
            del temporary_chat_contexts[chat_id]
    else:
        logger.warning(f"Falha ao salvar no Edge Config. Usando armazenamento temporário para o chat {chat_id}.")
        temporary_chat_contexts[chat_id] = context

# --- Inicialização dos Serviços ---
# API Gemini
genai.configure(api_key=GEMINI_API_KEY)
# O modelo agora é instanciado sob demanda com a configuração mais recente

# Aplicação python-telegram-bot
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
# Inicializa a aplicação para registrar os handlers e preparar para processar updates.
# Isso corrige o erro 'Application not initialized' no simulador.
asyncio.run(application.initialize())

# Aplicação Flask
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY


# --- Admin Web Interface ---

LOGIN_TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>Login - Bot Admin</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-100 text-slate-800">
  <div class="container mx-auto max-w-sm mt-20 p-8 bg-white rounded-lg shadow-lg">
    <h1 class="text-2xl font-bold text-center mb-6">Admin Login</h1>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          {% set color = 'bg-red-100 border-red-400 text-red-700' if category == 'error' else 'bg-blue-100 border-blue-400 text-blue-700' %}
          <div class="p-4 mb-4 text-sm rounded-lg border {{ color }}" role="alert">
            {{ message }}
          </div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    <form method=post>
      <div class="mb-4">
        <label for="username" class="block text-slate-700 text-sm font-bold mb-2">Username</label>
        <input type="text" id="username" name="username" required class="shadow appearance-none border rounded w-full py-2 px-3 text-slate-700 leading-tight focus:outline-none focus:shadow-outline">
      </div>
      <div class="mb-6">
        <label for="password" class="block text-slate-700 text-sm font-bold mb-2">Password</label>
        <input type="password" id="password" name="password" required class="shadow appearance-none border rounded w-full py-2 px-3 text-slate-700 leading-tight focus:outline-none focus:shadow-outline">
      </div>
      <div>
        <input type="submit" value="Login" class="w-full bg-blue-500 hover:bg-blue-700 text-white font-bold py-2 px-4 rounded focus:outline-none focus:shadow-outline cursor-pointer">
      </div>
    </form>
  </div>
</body>
</html>
"""

ADMIN_PANEL_TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>Bot Admin Panel</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-100 text-slate-800">
  <header class="bg-slate-800 text-white shadow-md">
    <div class="container mx-auto px-6 py-4 flex justify-between items-center">
      <h1 class="text-xl font-semibold">Bot Admin Panel</h1>
      <a href="{{ url_for('logout') }}" class="text-sm hover:text-slate-300">Logout</a>
    </div>
  </header>

  <main class="container mx-auto px-6 py-8">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          {% set colors = {
            'error': 'bg-red-100 border-red-500 text-red-700',
            'success': 'bg-green-100 border-green-500 text-green-700',
            'info': 'bg-blue-100 border-blue-500 text-blue-700'
          } %}
          <div class="p-4 mb-6 text-sm rounded-lg border {{ colors[category] or colors['info'] }}" role="alert">
            {{ message }}
          </div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">

      <!-- Coluna da Esquerda -->
      <div class="flex flex-col gap-8">
        <div class="bg-white p-6 rounded-lg shadow-lg">
          <h2 class="text-xl font-bold mb-4 border-b pb-2">System Status</h2>
          <div class="text-sm">{{ status_content | safe }}</div>
        </div>

        <div class="bg-white p-6 rounded-lg shadow-lg">
            <h2 class="text-xl font-bold mb-4 border-b pb-2">Webhook Tools</h2>
            <div class="flex items-center gap-4">
                <button id="check-webhook-btn" class="bg-blue-500 hover:bg-blue-700 text-white font-bold py-2 px-4 rounded">Check Status</button>
                <form action="{{ url_for('set_webhook') }}" method="post">
                    <input type="submit" value="Set Automatically" class="bg-amber-500 hover:bg-amber-600 text-white font-bold py-2 px-4 rounded cursor-pointer">
                </form>
            </div>
            <pre id="webhook-info-pre" class="mt-4 bg-slate-50 p-4 rounded-md text-xs text-slate-600 whitespace-pre-wrap word-wrap break-word">Click 'Check Status' to fetch webhook info...</pre>
        </div>

        <div class="bg-white p-6 rounded-lg shadow-lg">
          <h2 class="text-xl font-bold mb-4 border-b pb-2">Message Simulator (Text Only)</h2>
          {{ send_message_form | safe }}
        </div>

        <!-- AI Settings Card -->
        <div class="bg-white p-6 rounded-lg shadow-lg">
            <h2 class="text-xl font-bold mb-4 border-b pb-2">AI Configuration</h2>
            <form action="{{ url_for('save_settings') }}" method="post">
                <div class="mb-4">
                    <label for="system_instruction" class="block text-slate-700 text-sm font-bold mb-2">System Instruction:</label>
                    <textarea id="system_instruction" name="system_instruction" class="shadow-sm appearance-none border rounded w-full py-2 px-3 text-slate-700 leading-tight focus:outline-none focus:ring-2 focus:ring-blue-500" rows="4">{{ configs.system_instruction }}</textarea>
                </div>
                <div class="mb-4">
                    <h3 class="block text-slate-700 text-sm font-bold mb-2">Safety Filters</h3>
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {% for category, level in configs.safety_settings.items() %}
                        <div>
                            <label for="{{ category }}" class="text-xs font-semibold text-slate-600">{{ category.replace('HARM_CATEGORY_', '').replace('_', ' ')|title }}</label>
                            <select name="{{ category }}" id="{{ category }}" class="mt-1 shadow-sm border rounded w-full py-2 px-3 text-slate-700">
                                {% for option in ['BLOCK_NONE', 'BLOCK_LOW_AND_ABOVE', 'BLOCK_MEDIUM_AND_ABOVE', 'BLOCK_ONLY_HIGH'] %}
                                <option value="{{ option }}" {% if option == level %}selected{% endif %}>{{ option.replace('BLOCK_', '').replace('_', ' ')|title }}</option>
                                {% endfor %}
                            </select>
                        </div>
                        {% endfor %}
                    </div>
                </div>
                <input type="submit" value="Save AI Settings" class="w-full bg-emerald-500 hover:bg-emerald-700 text-white font-bold py-2 px-4 rounded cursor-pointer">
            </form>
        </div>
      </div>

      <!-- Coluna da Direita -->
      <div class="bg-white p-6 rounded-lg shadow-lg">
        <h2 class="text-xl font-bold mb-4 border-b pb-2">Chat with Gemini</h2>
        <div id="chat-history" class="h-96 overflow-y-auto border bg-slate-50 rounded-md p-4 mb-4 text-sm space-y-4">
          {{ chat_content | safe }}
        </div>
        <form action="{{ url_for('admin_chat') }}" method="post" class="flex gap-2">
          <input type="text" name="prompt" placeholder="Type your message..." required autocomplete="off" class="flex-grow shadow-sm appearance-none border rounded w-full py-2 px-3 text-slate-700 leading-tight focus:outline-none focus:ring-2 focus:ring-blue-500">
          <input type="submit" value="Send" class="bg-blue-500 hover:bg-blue-700 text-white font-bold py-2 px-4 rounded cursor-pointer">
        </form>
        <form action="{{ url_for('clear_chat') }}" method="post" class="mt-2">
          <input type="submit" value="Clear History" class="text-xs text-red-500 hover:text-red-700 underline cursor-pointer bg-transparent border-none p-0">
        </form>
      </div>

    </div>
  </main>
  <script>
    document.getElementById('check-webhook-btn').addEventListener('click', function() {
        const pre = document.getElementById('webhook-info-pre');
        pre.textContent = 'Fetching...';
        fetch('{{ url_for("get_webhook_info") }}')
            .then(response => {
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                pre.textContent = JSON.stringify(data, null, 2);
            })
            .catch(error => {
                pre.textContent = 'Error fetching webhook info: ' + error;
            });
    });
    const chatHistory = document.getElementById('chat-history');
    chatHistory.scrollTop = chatHistory.scrollHeight;
  </script>
</body>
</html>
"""

def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            flash("Por favor, faça login para acessar esta página.", "info")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Exibe o formulário de login e processa a autenticação."""
    if not all([ADMIN_USER, ADMIN_PASSWORD, FLASK_SECRET_KEY]):
        logger.error("As variáveis de ambiente do admin não estão configuradas.")
        return "<h1>Erro de Configuração</h1><p>A interface de administração não foi configurada corretamente no servidor. Contate o administrador.</p>", 500

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            flash("Login realizado com sucesso!", "success")
            return redirect(url_for('admin_panel'))
        else:
            flash("Credenciais inválidas. Tente novamente.", "error")

    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    """Faz o logout do usuário."""
    session.pop('logged_in', None)
    flash("Você foi desconectado.", "info")
    return redirect(url_for('login'))

async def get_webhook_info_data():
    """Busca as informações do webhook."""
    try:
        temp_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        async with temp_bot:
            webhook_info = await temp_bot.get_webhook_info()
            return webhook_info.to_dict()
    except Exception as e:
        logger.error(f"Falha ao buscar informações do webhook: {e}", exc_info=True)
        return {"error": str(e)}

@app.route('/admin/webhook_info')
@login_required
def get_webhook_info():
    """Endpoint da API para fornecer informações do webhook."""
    data = asyncio.run(get_webhook_info_data())
    return jsonify(data)

@app.route('/admin/set_webhook', methods=['POST'])
@login_required
def set_webhook():
    """Configura o webhook da aplicação no Telegram."""
    vercel_url = os.environ.get('VERCEL_URL')
    if not vercel_url:
        flash("A variável de ambiente VERCEL_URL não foi encontrada. Esta função só pode ser usada no ambiente da Vercel.", "error")
        return redirect(url_for('admin_panel'))

    webhook_url = f"https://{vercel_url}/{TELEGRAM_BOT_TOKEN}"

    async def do_set_webhook():
        """Helper assíncrono para configurar o webhook com uma instância de bot temporária."""
        try:
            logger.info(f"Configurando webhook para a URL: {webhook_url}")
            temp_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            async with temp_bot:
                success = await temp_bot.set_webhook(url=webhook_url)
            if success:
                flash(f"Webhook configurado com sucesso para: {webhook_url}", "success")
                logger.info("Webhook configurado com sucesso.")
            else:
                flash("A API do Telegram retornou uma falha ao configurar o webhook.", "error")
                logger.error("Falha ao configurar webhook, API retornou 'false'.")
        except Exception as e:
            logger.error(f"Falha ao configurar o webhook: {e}", exc_info=True)
            flash(f"Falha ao configurar o webhook: {e}", "error")

    asyncio.run(do_set_webhook())
    return redirect(url_for('admin_panel'))

@app.route('/admin/simulate', methods=['POST'])
@login_required
def simulate_message():
    """Simula o recebimento de uma mensagem do Telegram."""
    try:
        form_data = request.form
        message_type = form_data.get('message_type')

        # Construir um payload de atualização falso
        # Usamos um update_id e message_id aleatórios
        update_id = int(time.time() * 1000)
        message_id = int(time.time() * 1000)
        chat_id = int(form_data.get('chat_id'))
        user_id = int(form_data.get('user_id'))

        fake_update_payload = {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {
                    "id": chat_id,
                    "type": "private",
                    "username": form_data.get('username')
                },
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": form_data.get('username'),
                    "username": form_data.get('username')
                }
            }
        }

        if message_type == 'text':
            fake_update_payload['message']['text'] = form_data.get('text')
        else:
            flash(f"Tipo de mensagem simulada '{message_type}' ainda não é suportado.", "error")
            return redirect(url_for('admin_panel'))

        logger.info(f"Simulando uma atualização de mensagem: {json.dumps(fake_update_payload, indent=2)}")

        # Criar o objeto Update e processá-lo
        update = telegram.Update.de_json(fake_update_payload, application.bot)
        asyncio.run(application.process_update(update))

        flash("Mensagem de texto simulada foi enviada para o processador do bot.", "success")

    except Exception as e:
        logger.error(f"Erro ao simular mensagem: {e}", exc_info=True)
        flash(f"Erro ao simular a mensagem: {e}", "error")

    return redirect(url_for('admin_panel'))

async def check_api_status():
    """Verifica o status das conexões com as APIs do Telegram e Gemini."""
    status = {
        'telegram': {'status': 'Falha', 'details': 'Não foi possível verificar.'},
        'gemini': {'status': 'Falha', 'details': 'Não foi possível verificar.'}
    }
    # Verificar Telegram com uma instância de bot temporária para evitar conflitos de loop
    try:
        temp_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        async with temp_bot:
            bot_info = await temp_bot.get_me()
        status['telegram']['status'] = 'OK'
        status['telegram']['details'] = f"Conectado como @{bot_info.username} (ID: {bot_info.id})"
        logger.info("Verificação de status do Telegram: OK")
    except Exception as e:
        logger.error(f"Falha na verificação da API do Telegram: {e}", exc_info=True)
        status['telegram']['details'] = str(e)

    # Verificar Gemini
    try:
        if model:
            status['gemini']['status'] = 'OK'
            status['gemini']['details'] = f"Modelo '{model.model_name}' carregado com sucesso."
            logger.info("Verificação de status do Gemini: OK")
        else:
            status['gemini']['details'] = "O objeto do modelo não foi inicializado."
    except Exception as e:
        logger.error(f"Falha na verificação da API Gemini: {e}", exc_info=True)
        status['gemini']['details'] = str(e)

    return status

def format_status_html(status):
    """Formata o dicionário de status em HTML."""
    html_output = "<ul>"
    for service, info in status.items():
        icon = "✅" if info['status'] == 'OK' else "❌"
        # Corrigido: Usar o módulo 'html' importado e garantir que o detalhe é uma string.
        html_output += f"<li><strong>{service.title()}:</strong> {icon} {info['status']} - <small>{html.escape(str(info['details']))}</small></li>"
    html_output += "</ul>"
    return html_output

SEND_MESSAGE_FORM_TEMPLATE = """
<form action="{{ url_for('send_message') }}" method="post">
    <div class="mb-4">
        <label for="chat_id" class="block text-slate-700 text-sm font-bold mb-2">Chat ID:</label>
        <input type="text" id="chat_id" name="chat_id" required class="shadow-sm appearance-none border rounded w-full py-2 px-3 text-slate-700 leading-tight focus:outline-none focus:ring-2 focus:ring-blue-500">
    </div>
    <div class="mb-4">
        <label for="message" class="block text-slate-700 text-sm font-bold mb-2">Message:</label>
        <textarea id="message" name="message" required class="shadow-sm appearance-none border rounded w-full py-2 px-3 text-slate-700 leading-tight focus:outline-none focus:ring-2 focus:ring-blue-500" rows="3"></textarea>
    </div>
    <input type="submit" value="Send Message" class="bg-green-500 hover:bg-green-700 text-white font-bold py-2 px-4 rounded cursor-pointer">
</form>
"""

@app.route('/admin/send', methods=['POST'])
@login_required
def send_message():
    """Envia uma mensagem para um chat_id específico."""
    chat_id = request.form.get('chat_id')
    message = request.form.get('message')

    if not chat_id or not message:
        flash("Chat ID e Mensagem são obrigatórios.", "error")
        return redirect(url_for('admin_panel'))

    async def do_send():
        """Helper assíncrono para enviar a mensagem com uma instância de bot temporária."""
        try:
            logger.info(f"Enviando mensagem via painel admin para o chat {chat_id}...")
            temp_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            async with temp_bot:
                await temp_bot.send_message(chat_id=chat_id, text=message)
            flash(f"Mensagem enviada com sucesso para o Chat ID {chat_id}.", "success")
            logger.info("Mensagem enviada com sucesso.")
        except Exception as e:
            logger.error(f"Falha ao enviar mensagem via painel admin: {e}", exc_info=True)
            flash(f"Falha ao enviar mensagem: {e}", "error")

    asyncio.run(do_send())
    return redirect(url_for('admin_panel'))

@app.route('/admin/save_settings', methods=['POST'])
@login_required
def save_settings():
    """Salva as configurações da IA no Edge Config."""
    try:
        # Salvar a instrução de sistema
        system_instruction = request.form.get('system_instruction')
        save_config_item('system_instruction', system_instruction)

        # Montar e salvar as configurações de segurança
        safety_settings = {}
        for key, value in request.form.items():
            if key.startswith('HARM_CATEGORY_'):
                safety_settings[key] = value
        save_config_item('safety_settings', safety_settings)

        flash("Configurações da IA salvas com sucesso!", "success")
    except Exception as e:
        logger.error(f"Erro ao salvar configurações: {e}", exc_info=True)
        flash(f"Ocorreu um erro ao salvar as configurações: {e}", "error")

    return redirect(url_for('admin_panel'))

@app.route('/admin/chat', methods=['POST'])
@login_required
def admin_chat():
    """Processa uma mensagem do chat do painel de admin."""
    prompt = request.form.get('prompt')
    if not prompt:
        flash("A mensagem não pode estar vazia.", "error")
        return redirect(url_for('admin_panel'))

    chat_history = session.get('chat_history', [])

    try:
        configs = get_all_configs()
        # O chat do admin também deve usar as configurações
        # TODO: Adicionar lógica de contexto máximo aqui no futuro
        model = genai.GenerativeModel(
            'gemini-1.5-flash',
            system_instruction=configs.get('system_instruction'),
            safety_settings=configs.get('safety_settings')
        )

        logger.info(f"Enviando prompt do chat admin para a API Gemini: '{prompt}'")
        response = model.generate_content(prompt)
        ai_response = response.text
        logger.info("Resposta da API Gemini recebida para o chat admin.")

        chat_history.append({'role': 'user', 'text': prompt})
        chat_history.append({'role': 'model', 'text': ai_response})
        session['chat_history'] = chat_history

    except Exception as e:
        logger.error(f"Erro ao contatar a API Gemini no chat admin: {e}", exc_info=True)
        flash(f"Erro ao processar sua mensagem: {e}", "error")

    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_chat', methods=['POST'])
@login_required
def clear_chat():
    """Limpa o histórico de chat da sessão."""
    session.pop('chat_history', None)
    flash("Histórico do chat foi limpo.", "success")
    return redirect(url_for('admin_panel'))

def format_chat_html(history):
    """Formata o histórico do chat em HTML."""
    if not history:
        return "<p>Nenhuma mensagem ainda. Envie uma para começar!</p>"

    html_output = ""
    for message in history:
        role = message.get('role', 'unknown')
        text = html.escape(message.get('text', ''))
        html_output += f'<p class="{role}"><strong>{role.title()}:</strong><br>{text.replace(chr(10), "<br>")}</p>'
    return html_output

@app.route('/admin')
@login_required
def admin_panel():
    """Página principal do painel de administração."""
    # Rota síncrona que executa uma função assíncrona de forma segura.
    status_data = asyncio.run(check_api_status())
    status_content = format_status_html(status_data)

    # Formulário de envio de mensagem
    send_message_form = SEND_MESSAGE_FORM_TEMPLATE

    # Obter e formatar o histórico do chat
    chat_history = session.get('chat_history', [])
    chat_content = format_chat_html(chat_history)

    # Obter configurações atuais para preencher o formulário de configurações
    current_configs = get_all_configs()

    return render_template_string(
        ADMIN_PANEL_TEMPLATE,
        status_content=status_content,
        send_message_form=send_message_form,
        chat_content=chat_content,
        developer_chat_id=DEVELOPER_CHAT_ID,
        configs=current_configs
    )


# --- Helpers ---

async def send_safe_message(chat_id: int, text: str, **kwargs):
    """
    Envia uma mensagem de forma segura, criando uma instância de bot temporária
    para evitar conflitos de event loop no ambiente serverless.
    """
    try:
        temp_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        async with temp_bot:
            await temp_bot.send_message(chat_id=chat_id, text=text, **kwargs)
        logger.info(f"Mensagem enviada com sucesso para o chat {chat_id}.")
    except Exception as e:
        logger.error(f"Falha ao enviar mensagem segura para o chat {chat_id}: {e}", exc_info=True)

# --- Manipuladores de Mensagens (Handlers) ---

async def start(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """Envia uma mensagem de boas-vindas quando o comando /start é emitido."""
    chat_id = update.message.chat.id
    logger.info(f"Handler 'start' ativado para o chat {chat_id}.")
    welcome_message = (
        "Olá! Eu sou seu assistente multimodal com a tecnologia Gemini.\n\n"
        "Posso fazer o seguinte:\n"
        "- Conversar com você em texto.\n"
        "- Descrever imagens que você me enviar.\n"
        "- Transcrever mensagens de voz e arquivos de áudio.\n"
        "- Resumir vídeos.\n"
        "- Analisar e resumir documentos PDF.\n\n"
        "Basta me enviar qualquer um desses tipos de mídia e eu farei o meu melhor para ajudar!"
    )
    await send_safe_message(chat_id=chat_id, text=welcome_message)


async def clear_context(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """Limpa o histórico de conversas do usuário."""
    chat_id = update.message.chat.id
    logger.info(f"Handler 'clear' ativado para o chat {chat_id}.")
    context_key = f"context_{chat_id}"

    # Deletar do Edge Config
    deleted_from_edge = delete_config_item(context_key)

    # Deletar do fallback em memória
    if chat_id in temporary_chat_contexts:
        del temporary_chat_contexts[chat_id]
        logger.info(f"Contexto para o chat {chat_id} deletado do armazenamento temporário.")

    if deleted_from_edge:
        message = "Seu histórico de conversa foi limpo com sucesso."
    else:
        message = "Seu histórico de conversa foi limpo da memória temporária, mas pode não ter sido removido do armazenamento persistente."

    await send_safe_message(chat_id=chat_id, text=message)


async def handle_text(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    text = update.message.text
    logger.info(f"Handler 'text' ativado para o chat {chat_id}: '{text}'")

    try:
        # 1. Obter o histórico do chat
        chat_history = get_chat_context(chat_id)

        # 2. Adicionar a nova mensagem do usuário ao histórico
        # O formato esperado pela API do Gemini é uma lista de dicionários
        chat_history.append({"role": "user", "parts": [{"text": text}]})

        # 3. Limitar o tamanho do histórico para evitar exceder os limites da API
        # Manter as últimas 10 trocas (usuário + modelo)
        if len(chat_history) > 20:
            chat_history = chat_history[-20:]
            logger.info(f"Histórico para o chat {chat_id} truncado para 20 itens.")

        configs = get_all_configs()
        model = genai.GenerativeModel(
            'gemini-1.5-flash',
            system_instruction=configs.get('system_instruction'),
            safety_settings=configs.get('safety_settings')
        )

        # A API do Gemini espera um histórico que não termine com uma mensagem do usuário,
        # então removemos a última mensagem que acabamos de adicionar para passar para `start_chat`.
        initial_history = chat_history[:-1]

        # Iniciar uma sessão de chat com o histórico
        chat_session = model.start_chat(history=initial_history)

        logger.info(f"Enviando prompt com histórico para a API Gemini para o chat {chat_id}...")
        # Agora enviamos apenas a nova mensagem do usuário
        response = chat_session.send_message(text)
        logger.info(f"Resposta da API Gemini recebida para o chat {chat_id}.")

        # 4. A biblioteca `google-generativeai` atualiza `chat_session.history` automaticamente.
        # Apenas precisamos converter para um formato serializável em JSON.
        # O formato de `chat_session.history` já é o correto para ser usado na próxima chamada.
        serializable_history = [
            {'role': msg.role, 'parts': [{'text': part.text} for part in msg.parts]}
            for msg in chat_session.history
        ]

        # 5. Salvar o histórico atualizado
        save_chat_context(chat_id, serializable_history)

        await send_safe_message(chat_id=chat_id, text=response.text)
    except Exception as e:
        logger.error(f"Erro no handler de texto: {e}", exc_info=True)
        await send_safe_message(chat_id=chat_id, text="Desculpe, ocorreu um erro ao processar sua mensagem.")

async def handle_photo(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    logger.info(f"Handler 'photo' ativado para o chat {chat_id}.")

    try:
        await send_safe_message(chat_id=chat_id, text="Analisando a imagem...")
        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)

        f = io.BytesIO()
        await photo_file.download_to_memory(f)
        f.seek(0)

        img = PIL.Image.open(f)
        prompt_text = "Descreva esta imagem em detalhes. O que você vê?"

        configs = get_all_configs()
        model = genai.GenerativeModel(
            'gemini-1.5-flash',
            system_instruction=configs.get('system_instruction'),
            safety_settings=configs.get('safety_settings')
        )
        logger.info("Enviando imagem para a API Gemini...")
        response = model.generate_content([prompt_text, img])
        logger.info("Resposta da API Gemini recebida.")

        await send_safe_message(chat_id=chat_id, text=response.text)
    except Exception as e:
        logger.error(f"Erro no handler de foto: {e}", exc_info=True)
        await send_safe_message(chat_id=chat_id, text="Desculpe, ocorreu um erro ao analisar a imagem.")

async def handle_media(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE, media_type: str):
    chat_id = update.message.chat.id
    logger.info(f"Handler '{media_type}' ativado para o chat {chat_id}.")

    file_path = None
    gemini_file = None

    try:
        if media_type == 'audio':
            file_id = update.message.voice.file_id if update.message.voice else update.message.audio.file_id
            file_extension = "ogg" if update.message.voice else update.message.audio.file_name.split('.')[-1]
            prompt = "Transcreva o áudio deste arquivo na íntegra."
            processing_message = "Processando o áudio..."
        elif media_type == 'video':
            file_id = update.message.video.file_id
            file_extension = "mp4"
            prompt = "Resuma este vídeo em três pontos principais. Descreva o que acontece visualmente e o que é dito."
            processing_message = "Processando o vídeo... Isso pode levar alguns instantes."
        else:
            logger.warning(f"Tipo de mídia desconhecido em handle_media: {media_type}")
            return

        await send_safe_message(chat_id=chat_id, text=processing_message)

        tg_file = await context.bot.get_file(file_id)
        file_path = f"/tmp/{file_id}.{file_extension}"
        logger.info(f"Baixando arquivo para {file_path}...")
        await tg_file.download_to_drive(file_path)
        logger.info("Download concluído.")

        logger.info(f"Fazendo upload do arquivo {file_path} para a API Gemini File...")
        gemini_file = genai.upload_file(path=file_path)
        logger.info(f"Upload para a API Gemini concluído. File name: {gemini_file.name}")

        configs = get_all_configs()
        model = genai.GenerativeModel(
            'gemini-1.5-flash',
            system_instruction=configs.get('system_instruction'),
            safety_settings=configs.get('safety_settings')
        )
        logger.info(f"Enviando prompt de {media_type} para a API Gemini...")
        response = model.generate_content([prompt, gemini_file])
        logger.info(f"Resposta da API Gemini recebida.")

        await send_safe_message(chat_id=chat_id, text=f"Análise do {media_type}:\n{response.text}")

    except Exception as e:
        logger.error(f"Erro no handler de {media_type}: {e}", exc_info=True)
        await send_safe_message(chat_id=chat_id, text=f"Desculpe, ocorreu um erro ao processar o {media_type}.")

    finally:
        if file_path and os.path.exists(file_path):
            logger.info(f"Limpando arquivo temporário: {file_path}")
            os.remove(file_path)
        if gemini_file:
            logger.info(f"Deletando arquivo da API Gemini: {gemini_file.name}")
            genai.delete_file(gemini_file.name)

async def handle_document(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    document = update.message.document

    if document.mime_type != 'application/pdf':
        logger.warning(f"Usuário {chat_id} enviou arquivo com MimeType incorreto: {document.mime_type}")
        await send_safe_message(chat_id=chat_id, text="Por favor, envie um arquivo no formato PDF.")
        return

    logger.info(f"Handler 'document' (PDF) ativado para o chat {chat_id}: {document.file_name}")

    try:
        await send_safe_message(chat_id=chat_id, text=f"Analisando o PDF '{document.file_name}'...")

        logger.info("Baixando arquivo PDF para a memória...")
        doc_file = await context.bot.get_file(document.file_id)
        pdf_bytes = io.BytesIO()
        await doc_file.download_to_memory(pdf_bytes)
        pdf_bytes.seek(0)
        logger.info("Download do PDF para a memória concluído.")

        logger.info("Extraindo texto do PDF com PyMuPDF...")
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        extracted_text = "".join([page.get_text("text") for page in doc])
        logger.info(f"Texto extraído com sucesso. Total de {len(extracted_text)} caracteres.")

        if not extracted_text.strip():
            logger.warning(f"O PDF '{document.file_name}' não contém texto extraível.")
            await send_safe_message(chat_id=chat_id, text="O PDF parece não conter texto extraível.")
            return

        prompt = f"Resuma o seguinte texto extraído de um documento PDF. Identifique os pontos principais e conclusões:\n\n{extracted_text[:10000]}"

        configs = get_all_configs()
        model = genai.GenerativeModel(
            'gemini-1.5-flash',
            system_instruction=configs.get('system_instruction'),
            safety_settings=configs.get('safety_settings')
        )
        logger.info("Enviando texto extraído do PDF para a API Gemini...")
        response = model.generate_content(prompt)
        logger.info("Resposta da API Gemini recebida.")

        response_text = f"Resumo do PDF '{document.file_name}':\n\n{response.text}"

        logger.info(f"Enviando resumo do PDF para o chat {chat_id}.")
        for i in range(0, len(response_text), 4096):
            await send_safe_message(chat_id=chat_id, text=response_text[i:i+4096])
        logger.info("Resumo do PDF enviado com sucesso.")

    except Exception as e:
        logger.error(f"Erro no handler de documento: {e}", exc_info=True)
        await send_safe_message(chat_id=chat_id, text="Desculpe, ocorreu um erro ao analisar o PDF.")

# --- Manipulador de Erros ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Loga o erro e envia uma notificação para o desenvolvedor."""
    logger.error("Exceção ao manipular uma atualização:", exc_info=context.error)

    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    update_str = update.to_dict() if isinstance(update, telegram.Update) else str(update)

    # Trunca a mensagem para evitar o erro 'Message is too long'
    max_len = 3800 # Deixa uma margem de segurança
    error_details = (
        f"update = {json.dumps(update_str, indent=2, ensure_ascii=False)}\n\n"
        f"context.chat_data = {str(context.chat_data)}\n\n"
        f"context.user_data = {str(context.user_data)}\n\n"
        f"{tb_string}"
    )
    truncated_details = (error_details[:max_len] + '...') if len(error_details) > max_len else error_details

    message = (
        "Ocorreu uma exceção ao manipular uma atualização\n\n"
        f"<pre>{html.escape(truncated_details)}</pre>"
    )

    if DEVELOPER_CHAT_ID:
        await send_safe_message(
            chat_id=int(DEVELOPER_CHAT_ID), text=message, parse_mode=telegram.constants.ParseMode.HTML
        )

# --- Registro dos Handlers ---
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clear", clear_context))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, lambda u, c: handle_media(u, c, 'audio')))
application.add_handler(MessageHandler(filters.VIDEO, lambda u, c: handle_media(u, c, 'video')))
application.add_handler(MessageHandler(filters.Document.PDF, handle_document))
application.add_error_handler(error_handler)

# --- Endpoint do Webhook (Flask) ---
@app.route(f'/{TELEGRAM_BOT_TOKEN}', methods=['POST'])
def webhook():
    """Endpoint que recebe as atualizações do Telegram."""
    logger.info("--- Webhook Invocado ---")
    try:
        request_json = request.get_json(force=True)
        logger.info(f"Request JSON: {json.dumps(request_json, indent=2, ensure_ascii=False)}")

        update = telegram.Update.de_json(request_json, application.bot)
        logger.info("Update deserializado com sucesso.")

        asyncio.run(application.process_update(update))
        logger.info("Processamento do update concluído.")

    except json.JSONDecodeError:
        logger.error("Erro ao decodificar JSON do request.")
    except Exception as e:
        logger.error(f"Erro inesperado no webhook: {e}", exc_info=True)

    logger.info("--- Webhook Finalizado ---")
    return 'ok'

@app.route('/')
def index():
    return 'ok'
