import os
import logging
import asyncio
import html
import json
import traceback
import io

from flask import Flask, request, render_template_string, session, redirect, url_for, flash
import functools
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
import PIL.Image
import pymupdf  # fitz

# Carregar variáveis de ambiente do arquivo.env (para desenvolvimento local)
load_dotenv()

# --- Configuração ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Credenciais e IDs
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DEVELOPER_CHAT_ID = os.environ.get('DEVELOPER_CHAT_ID')

# Credenciais da Interface Web de Admin
FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY')
ADMIN_USER = os.environ.get('ADMIN_USER')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')


if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("As variáveis de ambiente TELEGRAM_BOT_TOKEN e GEMINI_API_KEY são obrigatórias.")

# --- Inicialização dos Serviços ---
# API Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Aplicação python-telegram-bot
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

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
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #f4f7f6; color: #333; line-height: 1.6; }
    .container { max-width: 400px; margin: 5em auto; padding: 2em; background: white; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); }
    h1 { text-align: center; color: #1a1a1a; }
    label { display: block; margin-bottom: .5em; font-weight: bold; }
    input[type=text], input[type=password] { width: 100%; padding: .8em; margin-bottom: 1em; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
    input[type=submit] { width: 100%; padding: .8em; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }
    input[type=submit]:hover { background: #0056b3; }
    .flash { padding: 1em; margin-bottom: 1em; border-radius: 4px; text-align: center; }
    .flash.error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
    .flash.info { background: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Admin Login</h1>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="flash {{ category }}">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    <form method=post>
      <label for=username>Username</label>
      <input type=text id=username name=username required>
      <label for=password>Password</label>
      <input type=password id=password name=password required>
      <input type=submit value=Login>
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
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #f4f7f6; color: #333; margin: 0; }
    .header { background: #2c3e50; color: white; padding: 1em; display: flex; justify-content: space-between; align-items: center; }
    .header h1 { margin: 0; font-size: 1.5em; }
    .header a { color: #ecf0f1; text-decoration: none; }
    .header a:hover { text-decoration: underline; }
    .container { padding: 2em; max-width: 900px; margin: 0 auto; }
    .card { background: white; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); margin-bottom: 2em; padding: 1.5em; }
    h2 { margin-top: 0; border-bottom: 2px solid #ecf0f1; padding-bottom: 0.5em; color: #2c3e50; }
    .flash { padding: 1em; margin-bottom: 1em; border-radius: 4px; text-align: center; }
    .flash.error { background: #e74c3c; color: white; }
    .flash.success { background: #2ecc71; color: white; }
    .flash.info { background: #3498db; color: white; }
  </style>
</head>
<body>
  <div class="header">
    <h1>Bot Admin Panel</h1>
    <a href="{{ url_for('logout') }}">Logout</a>
  </div>
  <div class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="flash {{ category }}">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="card" id="status-card">
      <h2>System Status</h2>
      {{ status_content | safe }}
    </div>

    <div class="card" id="send-message-card">
      <h2>Send Message to User</h2>
      {{ send_message_form | safe }}
    </div>

    <div class="card" id="logs-card">
      <h2>View Logs</h2>
      <p>A aplicação está rodando em um ambiente serverless. Os logs detalhados não podem ser exibidos aqui diretamente.</p>
      <p>Para ver os logs em tempo real, acesse o painel da sua aplicação na Vercel.</p>
      <a href="https://vercel.com/dashboard" target="_blank" rel="noopener noreferrer">Abrir Dashboard da Vercel</a>
    </div>
  </div>
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

async def check_api_status():
    """Verifica o status das conexões com as APIs do Telegram e Gemini."""
    status = {
        'telegram': {'status': 'Falha', 'details': 'Não foi possível verificar.'},
        'gemini': {'status': 'Falha', 'details': 'Não foi possível verificar.'}
    }
    # Verificar Telegram
    try:
        bot_info = await application.bot.get_me()
        status['telegram']['status'] = 'OK'
        status['telegram']['details'] = f"Conectado como @{bot_info.username} (ID: {bot_info.id})"
        logger.info("Verificação de status do Telegram: OK")
    except Exception as e:
        logger.error(f"Falha na verificação da API do Telegram: {e}")
        status['telegram']['details'] = str(e)

    # Verificar Gemini
    try:
        # A inicialização do modelo já é uma boa verificação.
        # Para uma verificação mais explícita, poderíamos listar modelos, mas isso adiciona latência.
        # Vamos assumir que se o 'model' existe, a configuração inicial foi bem-sucedida.
        if model:
            status['gemini']['status'] = 'OK'
            status['gemini']['details'] = f"Modelo '{model.model_name}' carregado com sucesso."
            logger.info("Verificação de status do Gemini: OK")
        else:
            status['gemini']['details'] = "O objeto do modelo não foi inicializado."
    except Exception as e:
        logger.error(f"Falha na verificação da API Gemini: {e}")
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
<style>
    form {{ display: flex; flex-direction: column; }}
    form label {{ margin-bottom: .5em; font-weight: 500; }}
    form input[type=text], form textarea {{
        width: 100%; padding: .8em; margin-bottom: 1em; border: 1px solid #ddd;
        border-radius: 4px; box-sizing: border-box; font-family: inherit;
    }}
    form textarea {{ resize: vertical; min-height: 80px; }}
    form input[type=submit] {{
        width: auto; padding: .8em 1.5em; background: #2ecc71; color: white; border: none;
        border-radius: 4px; cursor: pointer; font-size: 1em; align-self: flex-start;
    }}
    form input[type=submit]:hover {{ background: #27ae60; }}
</style>
<form action="{{ url_for('send_message') }}" method="post">
    <label for="chat_id">Chat ID:</label>
    <input type="text" id="chat_id" name="chat_id" required>
    <label for="message">Message:</label>
    <textarea id="message" name="message" required></textarea>
    <input type="submit" value="Send Message">
</form>
"""

@app.route('/admin/send', methods=['POST'])
@login_required
async def send_message():
    """Envia uma mensagem para um chat_id específico."""
    chat_id = request.form.get('chat_id')
    message = request.form.get('message')

    if not chat_id or not message:
        flash("Chat ID e Mensagem são obrigatórios.", "error")
        return redirect(url_for('admin_panel'))

    try:
        logger.info(f"Enviando mensagem via painel admin para o chat {chat_id}...")
        await application.bot.send_message(chat_id=chat_id, text=message)
        flash(f"Mensagem enviada com sucesso para o Chat ID {chat_id}.", "success")
        logger.info("Mensagem enviada com sucesso.")
    except Exception as e:
        logger.error(f"Falha ao enviar mensagem via painel admin: {e}", exc_info=True)
        flash(f"Falha ao enviar mensagem: {e}", "error")

    return redirect(url_for('admin_panel'))

@app.route('/admin')
@login_required
async def admin_panel():
    """Página principal do painel de administração."""
    # Verificar status das APIs
    # Corrigido: Usar await em uma rota async para evitar conflitos de event loop.
    status_data = await check_api_status()
    status_content = format_status_html(status_data)

    # Formulário de envio de mensagem
    send_message_form = SEND_MESSAGE_FORM_TEMPLATE

    return render_template_string(
        ADMIN_PANEL_TEMPLATE,
        status_content=status_content,
        send_message_form=send_message_form
    )


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
    await context.bot.send_message(chat_id=chat_id, text=welcome_message)
    logger.info(f"Mensagem de boas-vindas enviada para o chat {chat_id}.")


async def handle_text(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    text = update.message.text
    logger.info(f"Handler 'text' ativado para o chat {chat_id}: '{text}'")

    try:
        logger.info("Enviando prompt de texto para a API Gemini...")
        response = model.generate_content(text)
        logger.info("Resposta da API Gemini recebida.")

        await context.bot.send_message(chat_id=chat_id, text=response.text)
        logger.info(f"Resposta de texto enviada para o chat {chat_id}.")
    except Exception as e:
        logger.error(f"Erro no handler de texto: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="Desculpe, ocorreu um erro ao processar sua mensagem.")

async def handle_photo(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    logger.info(f"Handler 'photo' ativado para o chat {chat_id}.")

    try:
        await context.bot.send_message(chat_id=chat_id, text="Analisando a imagem...")
        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)

        f = io.BytesIO()
        await photo_file.download_to_memory(f)
        f.seek(0)

        img = PIL.Image.open(f)
        prompt_text = "Descreva esta imagem em detalhes. O que você vê?"

        logger.info("Enviando imagem para a API Gemini...")
        response = model.generate_content([prompt_text, img])
        logger.info("Resposta da API Gemini recebida.")

        await context.bot.send_message(chat_id=chat_id, text=response.text)
        logger.info(f"Descrição da imagem enviada para o chat {chat_id}.")
    except Exception as e:
        logger.error(f"Erro no handler de foto: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="Desculpe, ocorreu um erro ao analisar a imagem.")

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

        await context.bot.send_message(chat_id=chat_id, text=processing_message)

        tg_file = await context.bot.get_file(file_id)
        file_path = f"/tmp/{file_id}.{file_extension}"
        logger.info(f"Baixando arquivo para {file_path}...")
        await tg_file.download_to_drive(file_path)
        logger.info("Download concluído.")

        logger.info(f"Fazendo upload do arquivo {file_path} para a API Gemini File...")
        gemini_file = genai.upload_file(path=file_path)
        logger.info(f"Upload para a API Gemini concluído. File name: {gemini_file.name}")

        logger.info(f"Enviando prompt de {media_type} para a API Gemini...")
        response = model.generate_content([prompt, gemini_file])
        logger.info(f"Resposta da API Gemini recebida.")

        await context.bot.send_message(chat_id=chat_id, text=f"Análise do {media_type}:\n{response.text}")
        logger.info(f"Análise de {media_type} enviada para o chat {chat_id}.")

    except Exception as e:
        logger.error(f"Erro no handler de {media_type}: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text=f"Desculpe, ocorreu um erro ao processar o {media_type}.")

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
        await context.bot.send_message(chat_id=chat_id, text="Por favor, envie um arquivo no formato PDF.")
        return

    logger.info(f"Handler 'document' (PDF) ativado para o chat {chat_id}: {document.file_name}")

    try:
        await context.bot.send_message(chat_id=chat_id, text=f"Analisando o PDF '{document.file_name}'...")

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
            await context.bot.send_message(chat_id=chat_id, text="O PDF parece não conter texto extraível.")
            return

        prompt = f"Resuma o seguinte texto extraído de um documento PDF. Identifique os pontos principais e conclusões:\n\n{extracted_text[:10000]}" # Limita o tamanho do prompt

        logger.info("Enviando texto extraído do PDF para a API Gemini...")
        response = model.generate_content(prompt)
        logger.info("Resposta da API Gemini recebida.")

        response_text = f"Resumo do PDF '{document.file_name}':\n\n{response.text}"

        logger.info(f"Enviando resumo do PDF para o chat {chat_id}.")
        # Telegram tem um limite de 4096 caracteres por mensagem
        for i in range(0, len(response_text), 4096):
            await context.bot.send_message(chat_id=chat_id, text=response_text[i:i+4096])
        logger.info("Resumo do PDF enviado com sucesso.")

    except Exception as e:
        logger.error(f"Erro no handler de documento: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="Desculpe, ocorreu um erro ao analisar o PDF.")

# --- Manipulador de Erros ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exceção ao manipular uma atualização:", exc_info=context.error)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    update_str = update.to_dict() if isinstance(update, telegram.Update) else str(update)
    message = (
        "Ocorreu uma exceção ao manipular uma atualização\n\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )
    if DEVELOPER_CHAT_ID:
        await context.bot.send_message(
            chat_id=DEVELOPER_CHAT_ID, text=message, parse_mode=telegram.constants.ParseMode.HTML
        )

# --- Registro dos Handlers ---
application.add_handler(CommandHandler("start", start))
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
