import os
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict

load_dotenv()

class Config(BaseModel):
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMINS: List[int] = Field(default_factory=list)
    PAYMENT_TOKEN: str = os.getenv("PAYMENT_TOKEN", "")
    ROLLYPAY_API_KEY: str = os.getenv("ROLLYPAY_API_KEY", "")
    ROLLYPAY_TERMINAL_ID: str = os.getenv("ROLLYPAY_TERMINAL_ID", "")
    ROLLYPAY_SIGNING_SECRET: str = os.getenv("ROLLYPAY_SIGNING_SECRET", "")
    ROLLYPAY_TEST_MODE: bool = os.getenv("ROLLYPAY_TEST_MODE", "false").lower() == "true"

    # Настройки цен и скидок
    PRICES: Dict[int, Dict[str, int]] = {
        1: {"base_price": 250, "discount_percent": 0},
        3: {"base_price": 750, "discount_percent": 10},
        6: {"base_price": 1500, "discount_percent": 20},
        12: {"base_price": 3000, "discount_percent": 30}
    }

    @field_validator('ADMINS', mode='before')
    def parse_admins(cls, value):
        if isinstance(value, str):
            return [int(admin) for admin in value.split(",") if admin.strip()]
        return value or []

    def calculate_price(self, months: int) -> int:
        """Вычисляет итоговую стоимость с учетом скидки"""
        if months not in self.PRICES:
            return 0
        
        price_info = self.PRICES[months]
        base_price = price_info["base_price"]
        discount_percent = price_info["discount_percent"]
        
        discount_amount = (base_price * discount_percent) // 100
        return base_price - discount_amount

config = Config(
    ADMINS=os.getenv("ADMINS", "")
)
