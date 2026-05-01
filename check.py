import google.generativeai as genai

genai.configure(api_key="AIzaSyAmbtzh5KrPBvIAr2NYCnhSfGUE3wKcqqk")

for model in genai.list_models():
    print(model.name)