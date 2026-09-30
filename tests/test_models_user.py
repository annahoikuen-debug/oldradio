from retro_radio.models.user import User, PlanType

def test_user_creation():
    user = User(
        id="test-id",
        email="test@example.com",
        hashed_password="hashed",
        plan=PlanType.PREMIUM,
        generation_count=5
    )
    assert user.id == "test-id"
    assert user.email == "test@example.com"
    assert user.hashed_password == "hashed"
    assert user.plan == PlanType.PREMIUM
    assert user.generation_count == 5

def test_plan_type_enum():
    assert PlanType.FREE.value == "free"
    assert PlanType.PREMIUM.value == "premium"
    assert PlanType.PRO.value == "pro"
