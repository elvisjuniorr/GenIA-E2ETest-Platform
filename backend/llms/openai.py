from openai import OpenAI
import json
import re

def parse_json_output(text):

    if not text or not text.strip():
        raise Exception("OpenAI returned an empty response")

    text = text.strip()
    text = text.replace("```json", "").replace("```", "").strip()

    match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    return json.loads(text)

class OpenAIProvider:

    def generate_text(
        self,
        api_key,
        model,
        prompt
    ):

        client = OpenAI(
            api_key=api_key
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": prompt
                }
            ]
        )

        return response.choices[0].message.content

    def generate_json(
        self,
        api_key,
        model,
        prompt
    ):

        text = self.generate_text(
            api_key,
            model,
            prompt
        )

        try:
            return parse_json_output(text)
        except json.JSONDecodeError as e:
            raise Exception(
                f"Erro ao converter resposta OpenAI para JSON:\n{e}\n\nResposta:\n{text}"
            )

    def validate_connection(
        self,
        api_key,
        model
    ):

        client = OpenAI(
            api_key=api_key
        )

        client.models.list()
