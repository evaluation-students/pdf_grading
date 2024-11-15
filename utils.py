import hashlib
import json

def calculate_file_hash(file_data):
    hash_sha256 = hashlib.sha256()
    # If file_data is too large, process it in chunks
    for i in range(0, len(file_data), 4096):
        chunk = file_data[i:i+4096]
        hash_sha256.update(chunk)
    return hash_sha256.hexdigest()

def grade_submission(student_text, task_description, severity, preferences, chain):
    """
    Uses the LLM chain to grade the student's answer based on the task description.
    Returns a score between 0 and 100 along with feedback.
    """
    inputs = {
        "task_description": task_description,
        "student_text": student_text,
        "severity": severity,
        "preferences": preferences
    }
    response = chain.invoke(inputs)['text']
    response = response.replace('`', '')

    if response.startswith('json') or response.startswith('[]'):
        response = '\n'.join(response.split('\n')[1:])
    try:
        response = json.loads(response)
        return response['grade'], response['feedback']
    except json.JSONDecodeError:
        return 0, "Error parsing the response."


def update_grade(username, homework, grade, user_collection):
    user = user_collection.find_one({"username": username})

    if not user:
        return "User not found"

    try:
        homework_index = user['homework'].index(homework)
    except ValueError:
        return "Homework not found"

    if homework_index < len(user['grades']):
        user['grades'][homework_index] = grade
    else:
        user['grades'].append(grade)

    user_collection.update_one(
        {"_id": user['_id']},  # Find the document by user ID
        {"$set": {"grades": user['grades']}}  # Update the grades list
    )

    return "Grade updated successfully"