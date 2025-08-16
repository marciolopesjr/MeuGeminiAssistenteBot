import os
import logging
import asyncio
import html
import json
import traceback
import io

from flask import Flask, request
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, MessageHandler, filters, ContextTypes
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
DEVELOPER_CHAT_ID = os.environ.get('DEVELOPER_CHAT_ID') # Defina seu ID de chat como variável de ambiente

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY or not DEVELOPER_CHAT_ID:
    raise ValueError("Variáveis de ambiente TELEGRAM_BOT_TOKEN, GEMINI_API_KEY e DEVELOPER_CHAT_ID são obrigatórias.")

# --- Inicialização dos Serviços ---
# API Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Aplicação python-telegram-bot
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# Aplicação Flask
app = Flask(__name__)


# --- Manipuladores de Mensagens (Handlers) ---

async def handle_text(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    text = update.message.text
    logger.info(f"Processando texto de {chat_id}: '{text}'")

    response = model.generate_content(text)
    await context.bot.send_message(chat_id=chat_id, text=response.text)

async def handle_photo(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    logger.info(f"Processando foto de {chat_id}")

    photo_file = await context.bot.get_file(update.message.photo[-1].file_id)

    f = io.BytesIO()
    await photo_file.download_to_memory(f)
    f.seek(0)

    img = PIL.Image.open(f)
    prompt_text = "Descreva esta imagem em detalhes. O que você vê?"

    response = model.generate_content([prompt_text, img])
    await context.bot.send_message(chat_id=chat_id, text=response.text)

async def handle_media(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE, media_type: str):
    chat_id = update.message.chat.id
    logger.info(f"Processando {media_type} de {chat_id}")

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
        return

    file_path = f"/tmp/{file_id}.{file_extension}"
    gemini_file = None

    try:
        await context.bot.send_message(chat_id=chat_id, text=processing_message)
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(file_path)

        logger.info(f"Fazendo upload do arquivo {file_path} para a API Gemini.")
        gemini_file = genai.upload_file(path=file_path)

        response = model.generate_content([prompt, gemini_file])

        await context.bot.send_message(chat_id=chat_id, text=f"Análise do {media_type}:\n{response.text}")
        logger.info(f"Análise de {media_type} enviada para {chat_id}.")

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
        # Limpar o arquivo da API Gemini após o uso para gerenciar o armazenamento
        if gemini_file:
            genai.delete_file(gemini_file.name)

async def handle_document(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    document = update.message.document

    if document.mime_type!= 'application/pdf':
        await context.bot.send_message(chat_id=chat_id, text="Por favor, envie um arquivo no formato PDF.")
        return

    logger.info(f"Processando PDF de {chat_id}: {document.file_name}")
    await context.bot.send_message(chat_id=chat_id, text=f"Analisando o PDF '{document.file_name}'...")

    doc_file = await context.bot.get_file(document.file_id)
    pdf_bytes = io.BytesIO()
    await doc_file.download_to_memory(pdf_bytes)
    pdf_bytes.seek(0)

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    extracted_text = "".join([page.get_text("text") for page in doc])

    if not extracted_text.strip():
        await context.bot.send_message(chat_id=chat_id, text="O PDF parece não conter texto extraível.")
        return

    prompt = f"Resuma o seguinte texto extraído de um documento PDF. Identifique os pontos principais e conclusões:\n\n{extracted_text[:10000]}" # Limita o tamanho do prompt
    response = model.generate_content(prompt)

    response_text = f"Resumo do PDF:\n\n{response.text}"
    for i in range(0, len(response_text), 4096):
        await context.bot.send_message(chat_id=chat_id, text=response_text[i:i+4096])

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
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, lambda u, c: handle_media(u, c, 'audio')))
application.add_handler(MessageHandler(filters.VIDEO, lambda u, c: handle_media(u, c, 'video')))
application.add_handler(MessageHandler(filters.Document.PDF, handle_document))
application.add_error_handler(error_handler)

# --- Endpoint do Webhook (Flask) ---
@app.route(f'/{TELEGRAM_BOT_TOKEN}', methods=['POST'])
def webhook():
    update = telegram.Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return 'ok'

@app.route('/')
def index():
    return 'ok'
