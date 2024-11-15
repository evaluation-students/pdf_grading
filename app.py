import os
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient
from azure.ai.formrecognizer import DocumentAnalysisClient
from pymongo import MongoClient
from azure.core.credentials import AzureKeyCredential
from flask import Flask, request, jsonify, send_file
from utils import calculate_file_hash, grade_submission, update_grade
import base64
from azure.storage.blob import ContentSettings
from langchain.chat_models import ChatOpenAI
from langchain_core.prompts import PromptTemplate
from langchain.chains import LLMChain
from dotenv import load_dotenv
from flask_cors import CORS
import json
import pandas as pd
from io import BytesIO

load_dotenv()

DOCUMENT_INTELLIGENCE_ENDPOINT = os.getenv('DOCUMENT_INTELLIGENCE_ENDPOINT')
DOCUMENT_INTELLIGENCE_KEY = os.getenv('DOCUMENT_INTELLIGENCE_KEY')
STORAGE_CONNECTION_STRING = os.getenv('STORAGE_CONNECTION_STRING')
CONTAINER_NAME = "student-homework"
MONGO_STRING = "mongodb+srv://admin:admin@cluster0.1jic3.mongodb.net/evaluation"
os.environ["OPENAI_API_KEY"] = os.getenv('OPENAI_API_KEY')

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Initialize the BlobServiceClient and DocumentAnalysisClient
blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
document_analysis_client = DocumentAnalysisClient(endpoint=DOCUMENT_INTELLIGENCE_ENDPOINT,
                                                  credential=AzureKeyCredential(DOCUMENT_INTELLIGENCE_KEY))
client = MongoClient(MONGO_STRING)
db = client.get_database()
mongo_collection = db.pdf
user_collection = db.users

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
    if file.filename.lower().endswith('.txt'):
    # Move to the start of the file (in case it has been read from already)
        file.seek(0)

        # Read and decode the file content to text
        text = file.read().decode('utf-8')
    else:
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

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.0)
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

            # IMPORTANT INSTRUCTIONS #
            Make sure to follow the preferences provided by the teacher in `{preferences}`. They are **mandatory** and must directly impact the grading.

            For example, if the teacher specifies:
            - "If the answer is not in Romanian, deduct 20 points", make sure to apply this rule **before** grading the accuracy of the content.
            - If the teacher states any other preferences, you must follow them exactly.

            If no preferences are provided or they are empty, proceed with grading based on the correctness and clarity of the answer.

            ###########
            YOUR RESPONSE:
            """
    )

    chain = LLMChain(prompt=prompt, llm=llm)
    student_entries = list(mongo_collection.find({"homework": homework_name, "user_type": "student", "username": graded_username}))

    if not student_entries:
        return jsonify({"grade": 0, "feedback": "No homework"}), 200

    # Dictionary to store results
    student_text = ''

    # Grade each submission (file) for the given student
    for student in student_entries:
        student_text = student_text + '\n' + student['text']

    grade, feedback = grade_submission(student_text, task_description, severity, teacher_preferences, chain)

    if feedback == 'Error parsing the response.':
        return jsonify({"error": "There was an error at grading from the LLM, please try again"}), 400

    update_grade(graded_username, homework_name, grade, user_collection)

    # Return the results as JSON
    return jsonify({
        "grade": grade,
        "feedback": feedback
    })


@app.route("/export", methods=["GET"])
def export():
    # data = request.json
    homework_name = request.args.get('homework_name')
    if not homework_name:
        return jsonify({"error": "Please provide a homework_name parameter"}), 400

    # Query for students with the specific homework assignment
    query = {
        "role": "student",
        "homework": homework_name
    }

    # Retrieve matching documents
    results = user_collection.find(query)

    # Prepare the output list
    output = []

    # Process each document
    for doc in results:
        # Get the index of the homework_name in the homework array
        try:
            index = doc['homework'].index(homework_name)
            # Get the username and corresponding grade at the same index
            username = doc['username']
            grade = doc['grades'][index] if index < len(doc['grades']) else None

            # Add to output if a grade is found at the corresponding index
            if grade is not None:
                output.append({
                    "username": username,
                    "grade": grade
                })
        except ValueError:
            # Skip if homework_name is not in the homework list
            continue

    df = pd.DataFrame(output)

    # Save the DataFrame to a BytesIO object as an Excel file
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Students')

    # Set the pointer to the beginning of the BytesIO object
    output.seek(0)

    # Send the file for download
    return send_file(
        output,
        as_attachment=True,
        download_name='students.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


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
