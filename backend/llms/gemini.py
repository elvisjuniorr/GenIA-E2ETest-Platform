from google import genai
import json
import re


class GeminiProvider:

    def generate_text(
        self,
        api_key,
        model,
        prompt
    ):

        client = genai.Client(
            api_key=api_key
        )

        response = client.models.generate_content(
            model=model,
            contents=prompt
        )

        return response.text

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

        # Remove markdown
        text = text.replace(
            "```json",
            ""
        ).replace(
            "```",
            ""
        ).strip()

        # Extrai JSON válido
        match = re.search(
            r'(\{.*\}|\[.*\])',
            text,
            re.DOTALL
        )

        if match:
            text = match.group(1)

        try:
            return json.loads(text)

        except json.JSONDecodeError as e:
            raise Exception(
                f"Erro ao converter resposta Gemini para JSON:\n{e}\n\nResposta:\n{text}"
            )

    def validate_connection(
        self,
        api_key,
        model
    ):

        try:

            client = genai.Client(
                api_key=api_key
            )

            client.models.generate_content(
                model=model,
                contents="Connection test"
            )

            return True

        except Exception as e:
            raise Exception(
                f"Erro ao validar conexão Gemini: {str(e)}"
            )
