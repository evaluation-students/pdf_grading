import os
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient
from azure.ai.formrecognizer import DocumentAnalysisClient
from pymongo import MongoClient
from azure.core.credentials import AzureKeyCredential
from flask import Flask, request, jsonify
from utils import calculate_file_hash, grade_submission
import base64
from azure.storage.blob import ContentSettings
from langchain_openai import OpenAI
from langchain_core.prompts import PromptTemplate
from langchain.chains import LLMChain
from dotenv import load_dotenv

load_dotenv()

DOCUMENT_INTELLIGENCE_ENDPOINT = os.getenv('DOCUMENT_INTELLIGENCE_ENDPOINT')
DOCUMENT_INTELLIGENCE_KEY = os.getenv('DOCUMENT_INTELLIGENCE_KEY')
STORAGE_CONNECTION_STRING = os.getenv('STORAGE_CONNECTION_STRING')
CONTAINER_NAME = "student-homework"
MONGO_STRING = "mongodb+srv://admin:admin@cluster0.1jic3.mongodb.net/evaluation"
os.environ["OPENAI_API_KEY"] = os.getenv('OPENAI_API_KEY')

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

    file_hash = calculate_file_hash(file_data)
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
            "user_type": user_type,
            "homework": homework
        })

    return jsonify({"text": text}), 200


@app.route("/grade", methods=["POST"])
def grade():
    data = request.json
    homework_name = data.get("homework")
    graded_username = data.get("graded_username")
    teacher_preferences = data.get('preferences', [])
    severity = data.get('severity', "normal, not too severe, not too laid back")
    teacher_entry = mongo_collection.find_one({"homework": homework_name, "user_type": "teacher"})

    if teacher_entry:
        task_description = teacher_entry['text']
    else:
        raise ValueError("No teacher entry found for this homework.")

    llm = OpenAI()
    prompt = PromptTemplate(
        input_variables=["task_description", "student_text", "preferences", "severity"],
        template="""
            # CONTEXT #
            You are a grading assistant to a teacher. The teacher has to grade many students and you should try to help him. Each grade should be between 0 and 100, where 100 is the perfect answer.
            You will receive the task description for the task the students have to solve.
            You will receive the student answer.
            You will receive the teacher preferences and grading severity.

            #########

            # OBJECTIVE #
            Your task is to grade the students. The teacher might provide you with some preferences.
            Generally, the most important thing is the answer correctness.
            If the teacher provides preferences, then you MUST follow them.
            The teacher can also tell you how severe in grading you should be, if you should give out 100 points easily or not. If it wants you to be severe, then you should give 100 points only for the perfect answer.

            #########

            # STYLE #
            Write in an informative and instructional style.

            #########

            # Tone #
            Maintain a positive and motivational tone throughout, fostering a sense of empowerment and encouragement.

            # AUDIENCE #
            The target audience is students and teachers.

            #########

            # RESPONSE FORMAT #
            Please return the grade (a number between 0 and 100) and feedback in JSON format, as a string. I want only 2 fields, grade and feedback. The feedback should be an explanation for the grade.

            #############

            # VARIABLES #
            PLEASE START WITH PRINTING THIS: {preferences}

            Grading severity: {severity}

            Task: {task_description}

            Student answer: {student_text}'

            ###########
            YOUR RESPONSE:
            """
    )

    chain = LLMChain(prompt=prompt, llm=llm)
    student_entries = list(mongo_collection.find({"homework": homework_name, "user_type": "student", "username": graded_username}))

    if not student_entries:
        return jsonify({"error": f"No submissions found for student {graded_username} for homework {homework_name}"}), 404

    # Dictionary to store results
    student_text = ''

    # Grade each submission (file) for the given student
    for student in student_entries:
        student_text = student_text + '\n' + student['text']

    grade, feedback = grade_submission(student_text, task_description, severity, teacher_preferences, chain)

    if feedback == 'Error parsing the response.':
        return jsonify({"error": "There was an error at grading from the LLM, please try again"}), 400

    # Return the results as JSON
    return jsonify({
        "grade": grade,
        "feedback": feedback
    })


@app.route("/hello", methods=["POST"])
def print_hello():
    return jsonify({"result": 'hello'})


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
