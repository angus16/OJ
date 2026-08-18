import os
from google import genai
from google.genai import types
import anthropic
from openai import OpenAI

class CodingTutor:
    def __init__(self):
        self.gemini_key = os.environ.get("GEMINI_API_KEY", "")
        self.claude_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.openai_key = os.environ.get("OPENAI_API_KEY", "")

        try:
            self.gemini_client = genai.Client(api_key=self.gemini_key) if self.gemini_key else None
        except Exception:
            self.gemini_client = None

        try:
            self.claude_client = anthropic.Anthropic(api_key=self.claude_key) if self.claude_key else None
        except Exception:
            self.claude_client = None

        try:
            self.openai_client = OpenAI(api_key=self.openai_key) if self.openai_key else None
        except Exception:
            self.openai_client = None

        self.gemini_model = "gemini-2.5-flash"
        self.claude_model = "claude-haiku-4-5-20251001"
        self.openai_model = "gpt-4o-mini"

    def get_scaffolding_hint(self, student_code, error_msg, attempt_count):
        universal_system_instruction = (
            "你是一位專業且精準的資訊教育 AI 智慧家教。目前學生在線上評測系統（OJ）提交程式碼失敗了。\n"
            "【最高紅線】你絕對禁止、嚴格禁止提供任何修改好、正確的程式碼片段或標準答案（包含 ```python 等任何程式區塊）。\n"
            "【教學任務】請使用繁體中文。請遵循『數位鷹架（Scaffolding）』理論，給予漸進式、啟發式的引導。\n"
            "【極度重要：短小精悍】學生的注意力有限，請務必將你的提示控制在 3 個簡短的引導步驟或問題內，總字數嚴格限制在 150 字以內！直接切入痛點，拒絕廢話與長篇大論。"
        )

        if attempt_count <= 1:
            hint_level = "第一次嘗試，只給一個方向性問題，不能提示具體位置或符號。"
        elif attempt_count <= 3:
            hint_level = "已嘗試多次，可以指出錯誤的大概位置，但不能說出答案。"
        else:
            hint_level = "已嘗試超過3次，可以明確指出錯誤行號與問題類型，給出填空式引導，但絕對不能給完整程式碼。"

        user_prompt = (
            f"【學生程式碼】:\n{student_code}\n\n"
            f"【錯誤訊息】:\n{error_msg}\n\n"
            f"【已嘗試次數】: {attempt_count}\n"
            f"【提示深度要求】: {hint_level}\n\n"
            "請根據以上資訊，給予學生引導式提示，不得提供正確答案或修改後的程式碼。"
        )

        gemini_hint = self._get_gemini_hint(universal_system_instruction, user_prompt)
        claude_hint = self._get_claude_hint(universal_system_instruction, user_prompt)
        openai_hint = self._get_openai_hint(universal_system_instruction, user_prompt)

        return {
            "gemini": gemini_hint,
            "claude": claude_hint,
            "openai": openai_hint
        }

    def _get_gemini_hint(self, system_instruction, user_prompt):
        if not self.gemini_client:
            return "Gemini 未設定"
        try:
            response = self.gemini_client.models.generate_content(
                model=self.gemini_model,
                config=types.GenerateContentConfig(system_instruction=system_instruction),
                contents=user_prompt
            )
            return response.text
        except Exception as e:
            return "Gemini 錯誤: " + str(e)

    def _get_claude_hint(self, system_instruction, user_prompt):
        if not self.claude_client:
            return "Claude 未設定"
        try:
            response = self.claude_client.messages.create(
                model=self.claude_model,
                max_tokens=1024,
                system=system_instruction,
                messages=[
                    {"role": "user", "content": user_prompt}
                ]
            )
            return response.content[0].text
        except Exception as e:
            return "Claude 錯誤: " + str(e)

    def _get_openai_hint(self, system_instruction, user_prompt):
        if not self.openai_client:
            return "OpenAI 未設定"
        try:
            response = self.openai_client.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_prompt}
                ]
            )
            return response.choices[0].message.content
        except Exception as e:
            return "OpenAI 錯誤: " + str(e)
