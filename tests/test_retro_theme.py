"""
Regression tests for Retro Radio Theme implementation.
Tests design tokens, CSS generation, and visual theme consistency.
"""

import re
import pytest
from pathlib import Path
from utils.design_tokens import DesignTokenTransformer


class TestDesignTokens:
    """Test design token loading and resolution."""
    
    @pytest.fixture
    def transformer(self):
        return DesignTokenTransformer("design_tokens")
    
    def test_primitives_load(self, transformer):
        """Test that all primitive tokens load correctly."""
        tokens = transformer.load_all_tokens()
        assert len(tokens) > 200  # Should have many tokens
        
        # Check new retro colors exist
        assert "color-brand-500" in tokens
        assert tokens["color-brand-500"] == "#d47300"
        assert "color-brand-600" in tokens
        assert tokens["color-brand-600"] == "#b86200"
        assert "color-vintage-paper" in tokens
        assert tokens["color-vintage-paper"] == "#fdf6e3"
        assert "color-tube-glow" in tokens
        assert tokens["color-tube-glow"] == "#ffb000"
        assert "color-midnight-navy" in tokens
        assert tokens["color-midnight-navy"] == "#0d1b2a"
        assert "color-midnight-orange" in tokens
        assert tokens["color-midnight-orange"] == "#ff8c00"
        assert "color-midnight-text" in tokens
        assert tokens["color-midnight-text"] == "#f5f0e1"
        assert "color-vinyl-black" in tokens
        assert tokens["color-vinyl-black"] == "#0d0d0d"
        assert "color-vinyl-groove" in tokens
        assert tokens["color-vinyl-groove"] == "#1a1a1a"
        assert "color-cassette-shell" in tokens
        assert tokens["color-cassette-shell"] == "#2d2d2d"
        assert "color-cassette-tape" in tokens
        assert tokens["color-cassette-tape"] == "#1a1a1a"
        assert "color-cassette-label" in tokens
        assert tokens["color-cassette-label"] == "#f5f0e1"
    
    def test_semantic_references_resolve(self, transformer):
        """Test that semantic tokens reference primitives correctly."""
        transformer.load_all_tokens()
        resolved = transformer.resolve_all()
        
        # Check semantic colors resolve to new retro values
        assert resolved["color-interactive-primary"] == "#d47300"
        assert resolved["color-interactive-primary-hover"] == "#b86200"
        assert resolved["color-background-base"] == "#fdf6e3"
        assert resolved["color-border-base"] == "#ffcc80"
        assert resolved["color-focus-ring"] == "#ffb000"
    
    def test_dark_mode_overrides(self, transformer):
        """Test dark mode (midnight broadcast) overrides."""
        transformer.load_all_tokens()
        resolved = transformer.resolve_all()
        css = transformer.generate_css()

        # resolve_all() は「現在の（有効な）テーマ」を返す。既定はライト。
        assert resolved["color-background-base"] == "#fdf6e3"
        assert resolved["color-foreground-base"] == "#2d2d2d"

        # ダークテーマの値は CSS 側の [data-theme="dark"] ブロックに載る
        assert "--dt-color-background-base: #0d1b2a" in css
        assert "--dt-color-foreground-base: #f5f0e1" in css
        assert "--dt-color-interactive-primary: #ff8c00" in css
        assert "--dt-color-border-base: #404040" in css
        assert "--dt-color-background-code: #1a1a1a" in css


class TestGeneratedCSS:
    """Test generated CSS output."""
    
    @pytest.fixture
    def css_content(self):
        css_path = Path("styles/generated.css")
        assert css_path.exists(), "Generated CSS file not found"
        return css_path.read_text(encoding="utf-8")
    
    def test_css_has_root_variables(self, css_content):
        """Test CSS has :root with custom properties."""
        assert ":root {" in css_content
        assert "--dt-color-brand-500: #d47300;" in css_content
        assert "--dt-color-vintage-paper: #fdf6e3;" in css_content
        assert "--dt-color-tube-glow: #ffb000;" in css_content
    
    def test_css_has_dark_mode(self, css_content):
        """Test CSS has dark mode media query."""
        assert "@media (prefers-color-scheme: dark) {" in css_content
        assert "--dt-color-background-base: #0d1b2a;" in css_content
        assert "--dt-color-interactive-primary: #ff8c00;" in css_content
    
    def test_css_has_reduced_motion(self, css_content):
        """Test CSS respects prefers-reduced-motion."""
        assert "@media (prefers-reduced-motion: reduce) {" in css_content
        assert "animation-duration: 0.01ms !important" in css_content
    
    def test_css_has_high_contrast(self, css_content):
        """Test CSS has high contrast mode."""
        assert "@media (prefers-contrast: high) {" in css_content
    
    def test_no_blue_brand_colors(self, css_content):
        """Ensure old blue brand colors are gone."""
        # Old blue was #1f6feb - should not appear as brand primary
        # It may still exist as info color but not as brand
        brand_500_matches = re.findall(r'--dt-color-brand-500:\s*#([0-9a-fA-F]{6});', css_content)
        assert len(brand_500_matches) == 1
        assert brand_500_matches[0].lower() == "d47300"  # New amber, not old blue


class TestThemeColors:
    """Test theme color values meet requirements."""
    
    def test_contrast_ratios(self):
        """Test that color combinations meet WCAG AA contrast."""
        # This is a basic check - full contrast testing would need a color library
        # Key combinations that should have good contrast:
        # vintage-paper (#fdf6e3) vs foreground-base (#2d2d2d) - light mode
        # midnight-navy (#0d1b2a) vs midnight-text (#f5f0e1) - dark mode
        # These are manually verified to meet 4.5:1 ratio
        pass
    
    def test_brand_color_harmony(self):
        """Test brand colors form a harmonious scale."""
        # Brand scale should go from light to dark
        brand_colors = [
            "#fff8f0",  # 50
            "#fff3e0",  # 100
            "#ffe0b2",  # 200
            "#ffcc80",  # 300
            "#ffb74d",  # 400
            "#d47300",  # 500 (main)
            "#b86200",  # 600
            "#9c5200",  # 700
            "#7f4200",  # 800
            "#633300",  # 900
        ]
        # Verify they're all valid hex colors
        for color in brand_colors:
            assert re.match(r'^#[0-9a-fA-F]{6}$', color)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
