from langchain_openai import ChatOpenAI
import os
from dotenv import load_dotenv

def aiml_llm(model: str) -> ChatOpenAI:
    load_dotenv()

    return ChatOpenAI(

        base_url=os.getenv("AIML_URI"),
        api_key=os.getenv("AIML_API_KEY"),
        #base_url=os.getenv("FEATHERLESS_URI"),
        #api_key=os.getenv("FEATHERLESS_API_KEY"),    
        model=model,
    )
