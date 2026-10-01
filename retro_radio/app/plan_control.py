from typing import Tuple
from retro_radio.auth import Authenticator
from retro_radio.billing import PlanType, BillingCycle

class PlanController:
    def __init__(self):
        self.auth = Authenticator()
    
    def get_current_user(self):
        """現在のユーザーを取得"""
        return self.auth.get_current_user()
    
    def can_generate(self) -> Tuple[bool, int]:
        """生成可能かチェックし、(可能か, 残り回数) を返す"""
        user = self.get_current_user()
        if not user:
            return False, 0
        return self.auth.check_can_generate(user)
    
    def increment_generation(self):
        """生成回数をインクリメント"""
        user = self.get_current_user()
        if user:
            self.auth.increment_generation_count(user)
    
    def get_plan(self) -> PlanType:
        """現在のプランを取得"""
        user = self.get_current_user()
        if not user:
            return PlanType.FREE
        return self.auth.get_user_plan(user)
    
    def get_upgrade_url(self, plan: PlanType, cycle: BillingCycle = BillingCycle.MONTHLY) -> str:
        """アップグレードURLを取得"""
        user = self.get_current_user()
        if not user:
            raise ValueError("User not logged in")
        return self.auth.upgrade_to_premium(user, plan, cycle)
    
    def is_feature_enabled(self, feature: str) -> bool:
        """機能がプランで有効かチェック"""
        user = self.get_current_user()
        if not user:
            # 未ログイン時は基本機能のみ（原稿表示・基本音声再生）
            return feature in ["script_display", "audio_playback"]
        plan = self.auth.get_user_plan(user)
        # プラン別機能マッピング
        feature_map = {
            # 無料ユーザーでも利用可能
            "script_display": True,
            "audio_playback": True,
            # プレミアム以上
            "high_quality_audio": plan in [PlanType.PREMIUM, PlanType.PRO],
            "history_export": plan in [PlanType.PREMIUM, PlanType.PRO],
            "favorites": plan in [PlanType.PREMIUM, PlanType.PRO],
            # プロのみ
            "api_access": plan == PlanType.PRO,
            "batch_generation": plan == PlanType.PRO,
            "no_ads": plan in [PlanType.PREMIUM, PlanType.PRO],  # 今後の広告実装用
            # 無制限生成（月間生成制限は撤廃済みのため全プラン共通）
            "unlimited_generations": True,
        }
        return feature_map.get(feature, False)
