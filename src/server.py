import os
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient
from azure.ai.formrecognizer import DocumentAnalysisClient
from pymongo import MongoClient
from azure.core.credentials import AzureKeyCredential
from flask import Flask, request, jsonify
from utils import calculate_file_hash
import base64
from azure.storage.blob import ContentSettings



app = Flask(__name__)

# Initialize the BlobServiceClient and DocumentAnalysisClient
blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
document_analysis_client = DocumentAnalysisClient(endpoint=DOCUMENT_INTELLIGENCE_ENDPOINT,
                                                  credential=AzureKeyCredential(DOCUMENT_INTELLIGENCE_KEY))
client = MongoClient(MONGO_STRING)
db = client.get_database()
mongo_collection = db.pdf

@app.route("/upload", methods=["POST"])
def upload_file():
    # Get the file from the request
    file = request.files["file"]
    user_type = request.form.get("user_type")
    username = request.form.get("username")
    homework = request.form.get("homework")

    # Create a unique name for the file to store in blob storage
    if user_type == 'teacher':
        blob_name = homework + '/' + 'task' + '/' + file.filename
    else:
        blob_name = homework + '/' + username + '/' + file.filename

    file.seek(0)
    file_data = file.read()

    file_hash = calculate_file_hash(file)
    existing_file = mongo_collection.find_one({"file_hash": file_hash})

    if existing_file:
        extracted_text = existing_file["text"]
        return jsonify({"text": extracted_text}), 200

    # Get a reference to the Blob container
    container_client = blob_service_client.get_container_client(CONTAINER_NAME)

    # Upload the file to Blob Storage
    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(file_data, overwrite=True)

    # Get the URL of the uploaded file in Blob Storage
    blob_url = f"https://{blob_service_client.account_name}.blob.core.windows.net/{CONTAINER_NAME}/{blob_name}"

    # Now call Document Intelligence to extract text from the blob
    text = extract_text_from_document(blob_url)

    mongo_collection.insert_one({
            "file_hash": file_hash,
            "text": text,
            "filename": file.filename,
            "username": username,
            "user_type": user_type
        })

    return jsonify({"text": text}), 200


def extract_text_from_document(blob_url):
    # Call Document Intelligence to analyze the document
    poller = document_analysis_client.begin_analyze_document_from_url("prebuilt-read", blob_url)
    result = poller.result()

    # Extract text from the result
    extracted_text = ""
    for page in result.pages:
        for line in page.lines:
            extracted_text += line.content + "\n"

    return extracted_text

if __name__ == "__main__":
    app.run(debug=True)
