#!/usr/bin/env python3
"""
Design Token Transformation System

Reads JSON design tokens and converts them to CSS custom properties.
Supports token references (e.g., "{color-brand-500}") resolution.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional


class DesignTokenTransformer:
    """Transforms design tokens from JSON to CSS custom properties."""

    def __init__(self, tokens_dir: str = "design_tokens"):
        self.tokens_dir = Path(tokens_dir)
        self.tokens: Dict[str, Any] = {}
        self.resolved_tokens: Dict[str, str] = {}

    def load_all_tokens(self) -> Dict[str, Any]:
        """Load all token files from primitives, semantics, and components directories."""
        all_tokens = {}
        # Store primitives separately for reference resolution
        self.primitive_tokens = {}
        
        # Load in order: primitives -> semantics -> components
        for category in ["primitives", "semantics", "components"]:
            category_path = self.tokens_dir / category
            if not category_path.exists():
                continue
                
            for token_file in category_path.glob("*.json"):
                with open(token_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Extract properties from JSON schema format
                    if "properties" in data:
                        for key, value in data["properties"].items():
                            # Handle JSON Schema format with "value" field
                            if isinstance(value, dict) and "value" in value:
                                token_value = value["value"]
                                if category == "primitives":
                                    self.primitive_tokens[key] = token_value
                                # Don't overwrite primitives with semantic references
                                if key not in all_tokens or category == "primitives":
                                    all_tokens[key] = token_value
                            elif isinstance(value, dict) and "type" in value and value["type"] == "object":
                                # Handle nested objects like typography styles
                                if "properties" in value:
                                    nested = {}
                                    for sub_key, sub_value in value["properties"].items():
                                        if isinstance(sub_value, dict) and "value" in sub_value:
                                            nested[sub_key] = sub_value["value"]
                                        else:
                                            nested[sub_key] = sub_value
                                    if category == "primitives":
                                        self.primitive_tokens[key] = nested
                                    if key not in all_tokens or category == "primitives":
                                        all_tokens[key] = nested
                                else:
                                    if category == "primitives":
                                        self.primitive_tokens[key] = value
                                    if key not in all_tokens or category == "primitives":
                                        all_tokens[key] = value
                            else:
                                if category == "primitives":
                                    self.primitive_tokens[key] = value
                                if key not in all_tokens or category == "primitives":
                                    all_tokens[key] = value
                    else:
                        all_tokens.update(data)
                        if category == "primitives":
                            self.primitive_tokens.update(data)
        
        self.tokens = all_tokens
        return all_tokens

    def resolve_references(self, value: Any, visited: Optional[set] = None) -> Any:
        """Recursively resolve token references like {color-brand-500}."""
        if visited is None:
            visited = set()
            
        if isinstance(value, str):
            # Find all {token-name} patterns
            pattern = r'\{([^}]+)\}'
            matches = re.findall(pattern, value)
            
            result = value
            for match in matches:
                ref_key = match
                if ref_key in visited:
                    # Circular reference detected
                    continue
                visited.add(ref_key)
                
                # Check in tokens first, then primitive_tokens
                if ref_key in self.tokens:
                    resolved_value = self.resolve_references(self.tokens[ref_key], visited)
                    result = result.replace(f"{{{match}}}", str(resolved_value))
                elif ref_key in self.primitive_tokens:
                    resolved_value = self.resolve_references(self.primitive_tokens[ref_key], visited)
                    result = result.replace(f"{{{match}}}", str(resolved_value))
                else:
                    # Reference not found, keep as-is
                    pass
            return result
            
        elif isinstance(value, dict):
            return {k: self.resolve_references(v, visited) for k, v in value.items()}
            
        elif isinstance(value, list):
            return [self.resolve_references(v, visited) for v in value]
            
        return value

    def resolve_all(self) -> Dict[str, str]:
        """Resolve all token references and return flat dictionary of CSS custom properties."""
        self.load_all_tokens()
        
        # First pass: resolve simple values
        for key, value in self.tokens.items():
            if isinstance(value, (str, int, float, bool)):
                self.resolved_tokens[key] = str(self.resolve_references(value))
            elif isinstance(value, dict):
                # For nested objects (like typography), recursively flatten
                self._flatten_dict(key, value)
        
        return self.resolved_tokens

    def _flatten_dict(self, prefix: str, d: Dict[str, Any], visited: Optional[set] = None) -> None:
        """Recursively flatten nested dictionary into CSS custom properties."""
        if visited is None:
            visited = set()
            
        for key, value in d.items():
            css_key = f"{prefix}-{key}"
            if isinstance(value, (str, int, float, bool)):
                self.resolved_tokens[css_key] = str(self.resolve_references(value))
            elif isinstance(value, dict):
                self._flatten_dict(css_key, value, visited)

    def generate_css(self, output_path: Optional[str] = None, prefix: str = "dt") -> str:
        """Generate CSS custom properties from resolved tokens."""
        self.resolve_all()
        
        css_lines = [":root {"]
        
        # Group tokens by category for better organization
        categories = {
            "color": [],
            "spacing": [],
            "typography": [],
            "font": [],
            "line-height": [],
            "letter-spacing": [],
            "font-weight": [],
            "border": [],
            "shadow": [],
            "transition": [],
            "focus": [],
            "button": [],
            "form": [],
            "nav": [],
            "card": [],
            "panel": [],
            "other": []
        }
        
        for key, value in sorted(self.resolved_tokens.items()):
            css_var = f"--{prefix}-{key}"
            line = f"  {css_var}: {value};"
            
            # Categorize
            categorized = False
            for cat in categories:
                if key.startswith(cat):
                    categories[cat].append(line)
                    categorized = True
                    break
            if not categorized:
                categories["other"].append(line)
        
        # Output in order
        for cat in categories:
            if categories[cat]:
                css_lines.append(f"  /* {cat} */")
                css_lines.extend(categories[cat])
        
        css_lines.append("}")
        
        # Add dark mode overrides
        css_lines.append("")
        css_lines.append("@media (prefers-color-scheme: dark) {")
        css_lines.append("  :root {")
        
        # Dark mode color overrides - Midnight Broadcast theme
        dark_overrides = {
            "color-background-base": "color-midnight-navy",
            "color-background-elevated": "color-gray-900",
            "color-foreground-base": "color-midnight-text",
            "color-foreground-muted": "color-gray-400",
            "color-foreground-subtle": "color-gray-500",
            "color-border-base": "color-gray-700",
            "color-border-strong": "color-gray-600",
            "color-interactive-primary": "color-midnight-orange",
            "color-interactive-primary-hover": "#ffa500",
            "color-interactive-primary-active": "#e67e00",
            "color-interactive-primary-focus": "color-midnight-orange",
            "color-interactive-secondary": "color-gray-400",
            "color-interactive-secondary-hover": "color-gray-300",
            "color-interactive-secondary-active": "color-gray-200",
            "color-background-code": "color-gray-900",
        }
        
        # Resolve dark mode values from already resolved tokens
        for key, ref_key in dark_overrides.items():
            if ref_key in self.resolved_tokens:
                resolved = self.resolved_tokens[ref_key]
                css_lines.append(f"    --{prefix}-{key}: {resolved};")
            elif ref_key in self.tokens:
                resolved = self.resolve_references(self.tokens[ref_key])
                css_lines.append(f"    --{prefix}-{key}: {resolved};")
        
        css_lines.append("  }")
        css_lines.append("}")
        
        # Add high contrast mode overrides
        css_lines.append("")
        css_lines.append("@media (prefers-contrast: high) {")
        css_lines.append("  :root {")
        css_lines.append("    --dt-border-base: var(--dt-color-border-strong);")
        css_lines.append("    --dt-focus-ring-width: 4px;")
        css_lines.append("  }")
        css_lines.append("}")
        
        # Add reduced motion
        css_lines.append("")
        css_lines.append("@media (prefers-reduced-motion: reduce) {")
        css_lines.append("  *, *::before, *::after {")
        css_lines.append("    animation-duration: 0.01ms !important;")
        css_lines.append("    animation-iteration-count: 1 !important;")
        css_lines.append("    transition-duration: 0.01ms !important;")
        css_lines.append("  }")
        css_lines.append("}")
        
        css_content = "\n".join(css_lines)
        
        if output_path:
            output_dir = Path(output_path).parent
            output_dir.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(css_content)
        
        return css_content

    def generate_streamlit_injection(self) -> str:
        """Generate HTML style tag for Streamlit injection."""
        css = self.generate_css()
        return f'<style>\n{css}\n</style>'


def build_tokens(tokens_dir: str = "design_tokens", output_css: str = "styles/generated.css") -> str:
    """Build tokens and generate CSS file."""
    transformer = DesignTokenTransformer(tokens_dir)
    css = transformer.generate_css(output_css)
    return css


def get_tokens_for_streamlit(tokens_dir: str = "design_tokens") -> str:
    """Get tokens as HTML style tag for Streamlit."""
    transformer = DesignTokenTransformer(tokens_dir)
    return transformer.generate_streamlit_injection()


if __name__ == "__main__":
    import sys
    
    tokens_dir = sys.argv[1] if len(sys.argv) > 1 else "design_tokens"
    output_css = sys.argv[2] if len(sys.argv) > 2 else "styles/generated.css"
    
    transformer = DesignTokenTransformer(tokens_dir)
    css = transformer.generate_css(output_css)
    print(f"Generated CSS at {output_css}")
    print(f"Total tokens: {len(transformer.resolved_tokens)}")  
