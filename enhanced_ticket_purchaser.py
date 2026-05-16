#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
增强版独立购票脚本
完全配置驱动，包含完整的选座和购票流程
基于成功验证的Cloudflare绕过方法
"""

import sys
import os
import json
import time
import random
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from loguru import logger


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "enhanced_config.json"
ENV_PATH = BASE_DIR / ".env"
ARTIFACT_DIR = BASE_DIR / "artifacts"


def _parse_bool(value: Any, default: bool = False) -> bool:
    """Parse common truthy/falsy config values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_env_file(env_path: Path = ENV_PATH) -> Dict[str, str]:
    """Load a simple KEY=VALUE .env file without external dependencies."""
    loaded = {}
    if not env_path.exists():
        return loaded

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
        if key:
            loaded[key] = os.environ.get(key, value)
    return loaded


@dataclass
class TicketSelectionResult:
    """选票结果数据类"""
    success: bool
    selected_tickets: List[Dict] = None
    total_price: float = 0.0
    message: str = ""
    performance_id: str = ""


@dataclass
class PurchaseResult:
    """购票结果数据类"""
    success: bool
    order_id: str = ""
    total_amount: float = 0.0
    payment_status: str = ""
    message: str = ""


class ConfigDrivenTicketPurchaser:
    """配置驱动的独立购票系统"""
    
    def __init__(self, config_path="enhanced_config.json"):
        self.config_path = str(Path(config_path) if Path(config_path).is_absolute() else BASE_DIR / config_path)
        self.env = _load_env_file()
        self.config = self._load_config()
        self.driver = None
        self.current_event = None
        self.runtime_config = self.config.get('runtime', {})
        self.metrics = {}
        
        # 预编译高频使用的选择器
        self.fast_selectors = {
            'continue_btns': '.continue-btn, #continueBtn, button[onclick*="continue"], a[onclick*="continue"]',
            'login_btns': '.login-btn, #loginBtn, button[onclick*="login"], a[onclick*="login"]',
            'purchase_btns': '.load-button, #buyTicketBtn, .btn_cta, .purchase-btn',
            'go_buttons': 'button[onclick*="go("], a[onclick*="go("], button[onclick*="goEvent"], a[onclick*="goEvent"], button[onclick*="goPurchase"], a[onclick*="goPurchase"]'
        }
    
    def _normalize_whitespace(self, text: str) -> str:
        """Normalize full-width spaces to regular spaces and collapse multiple spaces."""
        text = text.replace('\u3000', ' ')  # full-width space -> regular space
        return ' '.join(text.split())

    def _fast_find_button(self, button_type: str, text_keywords: list = None, max_wait: int = 10) -> object:
        """持续快速查找按钮（不怕页面加载慢）"""
        try:
            check_interval = float(self._speed_value('fast_poll_interval', 0.15))
            total_rounds = int(max_wait / check_interval)
            
            for round_num in range(total_rounds):
                # 首先使用预编译的CSS选择器
                if button_type in self.fast_selectors:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, self.fast_selectors[button_type])
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            if not text_keywords:  # 如果不需要文本匹配
                                return element
                            
                            # 规范化文本后进行匹配
                            text = self._normalize_whitespace(element.text.strip())
                            if not text:
                                onclick = element.get_attribute('onclick') or ''
                                if onclick:
                                    onclick_normalized = onclick.replace('\u3000', ' ').lower()
                                    if any(keyword.lower() in onclick_normalized for keyword in text_keywords):
                                        return element
                            else:
                                # 全角空格归一化后进行匹配
                                if any(self._normalize_whitespace(keyword) in text for keyword in text_keywords):
                                    return element
                                # 如果直接匹配失败，也按字符匹配（忽略空格）
                                text_no_space = text.replace(' ', '')
                                if any(keyword.replace(' ', '').replace('\u3000', '') in text_no_space 
                                       for keyword in text_keywords 
                                       if self._normalize_whitespace(keyword)):
                                    return element
                
                # 备用：使用XPath查找（仅在必要时）
                if text_keywords:
                    # 将text中的全角空格替换后匹配
                    keyword_conditions = " or ".join([
                        f"contains(translate(text(), '\u3000', ' '), '{self._normalize_whitespace(kw)}')" 
                        for kw in text_keywords
                    ])
                    xpath = f"//button[{keyword_conditions}] | //a[{keyword_conditions}]"
                    elements = self.driver.find_elements(By.XPATH, xpath)
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            return element
                
                # 如果这轮没找到，短暂等待后继续
                if round_num < total_rounds - 1:  # 不是最后一轮
                    time.sleep(check_interval)
            
            return None
        except Exception as e:
            return None
        except Exception as e:
            return None
        
    def _load_config(self) -> Dict:
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config = self._merge_env_config(config)
            self._validate_config(config)
            print(f"✅ 成功加载配置文件: {self.config_path}")
            return config
        except FileNotFoundError:
            print(f"❌ 配置文件不存在: {self.config_path}")
            self._create_default_config()
            return self._load_config()
        except Exception as e:
            print(f"❌ 配置文件加载失败: {e}")
            sys.exit(1)

    def _wait_until(self, condition, timeout: Optional[int] = None, interval: float = 0.25, label: str = "condition") -> Any:
        """Small polling helper for fast, responsive waits."""
        timeout = timeout or self.config.get('browser_config', {}).get('page_timeout', 30)
        if interval == 0.25:
            interval = float(self.config.get('performance_tuning', {}).get('normal_poll_interval', 0.35))
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                result = condition()
                if result:
                    return result
            except Exception as exc:
                last_error = exc
            time.sleep(interval)
        if last_error and self.config.get('runtime', {}).get('verbose'):
            print(f"⚠️ 等待 {label} 超时，最后错误: {last_error}")
        return None

    def _wait_for_page_ready(self, timeout: Optional[int] = None) -> bool:
        """Wait until document.readyState is interactive or complete."""
        strategy = self.config.get('performance_tuning', {}).get('page_ready_strategy', 'eager')
        accepted_states = {"interactive", "complete"} if strategy == 'eager' else {"complete"}
        def ready():
            state = self.driver.execute_script("return document.readyState")
            return state in accepted_states
        return bool(self._wait_until(ready, timeout=timeout, label="页面加载"))

    def _speed_value(self, key: str, default: Any) -> Any:
        """Read speed/performance tuning values."""
        return self.config.get('performance_tuning', {}).get(key, default)

    def _mark_metric(self, name: str) -> None:
        """Record lightweight timing checkpoints for performance tuning."""
        if self.config.get('performance_tuning', {}).get('enabled', True):
            self.metrics[name] = round(time.time(), 3)

    def _save_metrics(self) -> None:
        """Persist timing metrics without slowing the hot path."""
        if not self.config.get('performance_tuning', {}).get('enabled', True) or not self.metrics:
            return
        try:
            ARTIFACT_DIR.mkdir(exist_ok=True)
            started = self.metrics.get('flow_start') or min(self.metrics.values())
            data = {
                'metrics': self.metrics,
                'elapsed': {key: round(value - started, 3) for key, value in self.metrics.items()}
            }
            (ARTIFACT_DIR / 'runtime_metrics.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _safe_click(self, element, label: str = "元素") -> bool:
        """Scroll and click an element with JS fallback."""
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});", element)
            try:
                element.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", element)
            print(f"✅ 已点击{label}")
            return True
        except Exception as e:
            print(f"❌ 点击{label}失败: {e}")
            return False

    def _normalize_whitespace(self, text: str) -> str:
        """Normalize full-width spaces and other whitespace to regular spaces."""
        if not text:
            return ""
        text = text.replace("\u3000", " ")
        return " ".join(text.split())

    def _normalize_text(self, text: str) -> str:
        """Normalize text for strategy matching."""
        return re.sub(r"\s+", " ", text or "").strip().lower()

    def _keywords_score(self, text: str, rules: Any, default_weight: int = 20) -> int:
        """Score text with string/list/dict keyword rules."""
        normalized = self._normalize_text(text)
        if not rules:
            return 0
        if isinstance(rules, str):
            rules = [rules]

        score = 0
        for rule in rules:
            if isinstance(rule, dict):
                keywords = rule.get('keywords') or rule.get('keyword') or []
                weight = int(rule.get('weight', default_weight))
            else:
                keywords = rule
                weight = default_weight
            if isinstance(keywords, str):
                keywords = [keywords]
            if keywords and all(self._normalize_text(keyword) in normalized for keyword in keywords):
                score += weight
        return score

    def _contains_any_keyword(self, text: str, keywords: Any) -> bool:
        """Return true if text contains any configured keyword."""
        if isinstance(keywords, str):
            keywords = [keywords]
        normalized = self._normalize_text(text)
        return any(self._normalize_text(keyword) in normalized for keyword in (keywords or []))

    def _detect_sale_state(self, text: Optional[str] = None) -> str:
        """Detect sale state from current page or a candidate element."""
        strategy = self.config.get('target_event', {}).get('sale_state_strategy', {})
        source = text if text is not None else f"{self.driver.title}\n{self.driver.current_url}\n{self.driver.page_source[:50000]}"

        if text is None and self._has_clickable_purchase_entry(strategy):
            return 'clickable'

        state_keywords = [
            ('stop', strategy.get('stop_when', ['售罄', 'Sold Out', '取消', 'Cancelled'])),
            ('refresh', strategy.get('refresh_when', ['繁忙', 'Busy', '系統忙碌', '系统忙碌'])),
            ('wait', strategy.get('wait_when', ['即將發售', '即将发售', 'Coming Soon', '稍後開售']))
        ]
        for state, keywords in state_keywords:
            if self._contains_any_keyword(source, keywords):
                return state
        return 'unknown'

    def _has_clickable_purchase_entry(self, strategy: Dict) -> bool:
        """Require a real clickable element before classifying a page as purchasable."""
        click_keywords = strategy.get('click_when', ['前往購票', '前往购票', '立即購買', '立即购买', 'Buy Tickets', 'Purchase'])
        selectors = [
            '.buyTicketBox button', '.buyTicketBox a', '.waitListTicketBox button', '.waitListTicketBox a',
            'button[onclick*="go"]', 'a[onclick*="go"]', 'a[href*="venue.cityline.com"]',
            '.load-button', '#buyTicketBtn', '.btn_cta', '.purchase-btn'
        ]
        for selector in selectors:
            try:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    if not element.is_displayed() or not element.is_enabled():
                        continue
                    text = element.text.strip() or element.get_attribute('aria-label') or element.get_attribute('value') or ''
                    href = element.get_attribute('href') or ''
                    onclick = element.get_attribute('onclick') or ''
                    combined = f"{text} {href} {onclick}"
                    if selector in {'.load-button', '#buyTicketBtn', '.btn_cta', '.purchase-btn'} or self._contains_any_keyword(combined, click_keywords):
                        return True
            except Exception:
                continue
        return False

    def _save_discovery_result(self, name: str, data: Any) -> None:
        """Persist discovery scan data for tuning strategies."""
        discovery = self.config.get('target_event', {}).get('discovery_mode', {})
        if not discovery.get('enabled', True):
            return
        try:
            ARTIFACT_DIR.mkdir(exist_ok=True)
            output = ARTIFACT_DIR / discovery.get('options_file', f'{name}.json')
            if not output.is_absolute():
                output = ARTIFACT_DIR / output.name
            output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"🧭 已保存扫描结果: {output}")
        except Exception as e:
            print(f"⚠️ 保存扫描结果失败: {e}")

    def _capture_diagnostics(self, name: str, include_html: bool = True) -> Optional[Path]:
        """Save screenshot and compact page metadata for debugging failures."""
        if not self.driver or not self.config.get('runtime', {}).get('diagnostics', True):
            return None

    def _collect_browser_fingerprint(self) -> Dict[str, Any]:
        """Collect browser environment details useful for Cloudflare diagnosis."""
        if not self.driver:
            return {}
        try:
            return self.driver.execute_script("""
                return {
                    userAgent: navigator.userAgent,
                    webdriver: navigator.webdriver,
                    language: navigator.language,
                    languages: navigator.languages,
                    platform: navigator.platform,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    cookieEnabled: navigator.cookieEnabled,
                    hardwareConcurrency: navigator.hardwareConcurrency,
                    deviceMemory: navigator.deviceMemory || null
                };
            """) or {}
        except Exception as e:
            return {"error": str(e)}

    def _capture_cloudflare_diagnostics(self, name: str) -> Optional[Path]:
        """Save Cloudflare-focused diagnostics including iframe and browser details."""
        if not self.driver or not self.config.get('runtime', {}).get('diagnostics', True):
            return None
        try:
            ARTIFACT_DIR.mkdir(exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            safe_name = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '_' for ch in name)
            base = ARTIFACT_DIR / f"{timestamp}_{safe_name}"
            iframes = []
            for iframe in self.driver.find_elements(By.TAG_NAME, "iframe"):
                try:
                    iframes.append({
                        "src": iframe.get_attribute("src") or "",
                        "title": iframe.get_attribute("title") or "",
                        "displayed": iframe.is_displayed()
                    })
                except Exception:
                    continue
            browser_config = self.config.get('browser_config', {})
            metadata = {
                "url": self.driver.current_url,
                "title": self.driver.title,
                "timestamp": timestamp,
                "page_source_length": len(self.driver.page_source or ""),
                "iframes": iframes,
                "fingerprint": self._collect_browser_fingerprint(),
                "runtime_speed_mode": self.config.get('runtime', {}).get('speed_mode'),
                "cloudflare_safe_mode": self.config.get('cloudflare', {}).get('safe_mode'),
                "disable_images_in_fast_mode": browser_config.get('disable_images_in_fast_mode'),
                "disable_background_networking": browser_config.get('disable_background_networking'),
                "disable_extensions_in_fast_mode": browser_config.get('disable_extensions_in_fast_mode'),
                "user_data_dir": browser_config.get('user_data_dir', '')
            }
            (base.with_suffix(".json")).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            if self.config.get('runtime', {}).get('save_screenshots', True):
                self.driver.save_screenshot(str(base.with_suffix(".png")))
            html = self.driver.page_source or ""
            (base.with_suffix(".html")).write_text(html[:300000], encoding="utf-8", errors="ignore")
            print(f"📎 已保存Cloudflare诊断: {base.name}.*")
            return base
        except Exception as e:
            print(f"⚠️ 保存Cloudflare诊断失败: {e}")
            return None
        speed_mode = self.config.get('runtime', {}).get('speed_mode')
        skip_heavy = self.config.get('performance_tuning', {}).get('skip_noncritical_diagnostics_in_fast_mode', True)
        if speed_mode in {'fast', 'turbo'} and skip_heavy and name not in {'purchase_button_not_found', 'ticket_selection_failed', 'auto_submit_failed'}:
            include_html = False
        try:
            ARTIFACT_DIR.mkdir(exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            safe_name = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '_' for ch in name)
            base = ARTIFACT_DIR / f"{timestamp}_{safe_name}"

            metadata = {
                "url": self.driver.current_url,
                "title": self.driver.title,
                "timestamp": timestamp,
                "page_source_length": len(self.driver.page_source or "")
            }
            (base.with_suffix(".json")).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

            if self.config.get('runtime', {}).get('save_screenshots', True):
                self.driver.save_screenshot(str(base.with_suffix(".png")))
            if include_html:
                html = self.driver.page_source or ""
                (base.with_suffix(".html")).write_text(html[:300000], encoding="utf-8", errors="ignore")

            print(f"📎 已保存诊断快照: {base.name}.*")
            return base
        except Exception as e:
            print(f"⚠️ 保存诊断快照失败: {e}")
            return None

    def _merge_env_config(self, config: Dict) -> Dict:
        """Merge optional .env values into runtime config."""
        config.setdefault('member_info', {})
        config.setdefault('notifications', {})
        config.setdefault('purchase_settings', {})
        config.setdefault('browser_config', {})
        config.setdefault('runtime', {})

        env_mappings = {
            'CITYLINE_EMAIL': ('member_info', 'username'),
            'CITYLINE_PASSWORD': ('member_info', 'password'),
            'NOTIFICATION_WEBHOOK_URL': ('notifications', 'webhook_url'),
            'CHROME_BINARY_PATH': ('browser_config', 'binary_location'),
            'CHROME_USER_DATA_DIR': ('browser_config', 'user_data_dir'),
            'DEBUG_MODE': ('runtime', 'debug'),
            'VERBOSE_LOGGING': ('runtime', 'verbose'),
            'SAVE_SCREENSHOTS': ('runtime', 'save_screenshots'),
            'NON_INTERACTIVE': ('runtime', 'non_interactive')
        }

        for env_key, (section, key) in env_mappings.items():
            value = os.getenv(env_key)
            if value is None or value == "":
                continue
            if key in {'debug', 'verbose', 'save_screenshots', 'non_interactive'}:
                value = _parse_bool(value)
            config.setdefault(section, {})[key] = value

        if config['member_info'].get('username') and config['member_info'].get('password'):
            config['member_info']['auto_login'] = config['member_info'].get('auto_login', True)

        return config

    def _validate_config(self, config: Dict) -> None:
        """Validate and normalize config values early."""
        url = config.get('target_event', {}).get('url', '')
        if not url or not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            raise ValueError("target_event.url 必须是有效的 http(s) URL")

        ticket_prefs = config.setdefault('ticket_preferences', {})
        quantity = int(ticket_prefs.get('quantity', 1) or 1)
        ticket_prefs['quantity'] = max(1, min(quantity, 10))
        zones = ticket_prefs.get('preferred_zones', [])
        if isinstance(zones, str):
            zones = [zones]
        ticket_prefs['preferred_zones'] = zones or ['VIP', 'A区', 'B区']

        purchase = config.setdefault('purchase_settings', {})
        purchase['auto_purchase'] = _parse_bool(purchase.get('auto_purchase'), False)
        purchase['auto_submit_order'] = _parse_bool(
            purchase.get('auto_submit_order'),
            purchase.get('auto_purchase', False)
        )
        purchase['max_wait_time'] = int(purchase.get('max_wait_time', 300) or 300)

        runtime = config.setdefault('runtime', {})
        runtime['diagnostics'] = _parse_bool(runtime.get('diagnostics'), True)
        runtime['save_screenshots'] = _parse_bool(runtime.get('save_screenshots'), True)
        runtime['non_interactive'] = _parse_bool(runtime.get('non_interactive'), False)
        runtime['verbose_login_status'] = _parse_bool(runtime.get('verbose_login_status'), False)
        runtime.setdefault('speed_mode', 'fast')

        performance = config.setdefault('performance_tuning', {})
        performance['enabled'] = _parse_bool(performance.get('enabled'), True)
        performance.setdefault('fast_poll_interval', 0.15)
        performance.setdefault('normal_poll_interval', 0.35)
        performance.setdefault('slow_poll_interval', 1.0)
        performance.setdefault('button_search_timeout', 6)
        performance.setdefault('post_click_wait', 0.25)
        performance.setdefault('page_ready_strategy', 'eager')
        performance['skip_noncritical_diagnostics_in_fast_mode'] = _parse_bool(
            performance.get('skip_noncritical_diagnostics_in_fast_mode'), True
        )

        cloudflare = config.setdefault('cloudflare', {})
        cloudflare['manual_wait_seconds'] = int(cloudflare.get('manual_wait_seconds', 300) or 300)
        cloudflare['check_interval'] = float(cloudflare.get('check_interval', 1.5) or 1.5)
        cloudflare['keep_browser_open_on_failure'] = _parse_bool(cloudflare.get('keep_browser_open_on_failure'), True)
        cloudflare['safe_mode'] = _parse_bool(cloudflare.get('safe_mode'), True)

        browser = config.setdefault('browser_config', {})
        browser.setdefault('headless', False)
        browser.setdefault('page_timeout', 30)
        browser.setdefault('stealth_mode', True)
        browser.setdefault('window_size', [1920, 1080])
        browser['use_subprocess'] = _parse_bool(browser.get('use_subprocess'), False)
        browser['disable_images_in_fast_mode'] = _parse_bool(browser.get('disable_images_in_fast_mode'), False)
        browser['disable_background_networking'] = _parse_bool(browser.get('disable_background_networking'), False)
        browser['disable_extensions_in_fast_mode'] = _parse_bool(browser.get('disable_extensions_in_fast_mode'), False)
        browser.setdefault('accept_language', 'zh-HK,zh-TW,zh-CN,en-US,en')
        browser.setdefault('user_agent', '')
        if browser.get('user_data_dir'):
            user_data_dir = Path(str(browser['user_data_dir']))
            if not user_data_dir.is_absolute():
                user_data_dir = BASE_DIR / user_data_dir
            browser['user_data_dir'] = str(user_data_dir)

        target = config.setdefault('target_event', {})
        if 'urls' not in target:
            target['urls'] = [target.get('url')] if target.get('url') else []

        target.setdefault('product_strategy', {
            'mode': 'current_url',
            'fallback_to_current_url': True
        })
        target.setdefault('session_strategy', {
            'mode': 'first_available',
            'fallback': 'first_available',
            'allow_any_session_if_preferred_unavailable': True
        })
        target.setdefault('sale_state_strategy', {
            'detect_states': True,
            'click_when': ['前往購票', '前往购票', '立即購買', '立即购买', 'Buy Tickets', 'Purchase'],
            'wait_when': ['即將發售', '即将发售', 'Coming Soon', '稍後開售'],
            'stop_when': ['售罄', 'Sold Out', '取消', 'Cancelled'],
            'refresh_when': ['繁忙', 'Busy', '系統忙碌', '系统忙碌'],
            'poll_until_on_sale': False,
            'pre_start_poll_interval': 1.5,
            'on_start_poll_interval': 0.3,
            'max_poll_seconds': 120
        })
        target.setdefault('discovery_mode', {
            'enabled': True,
            'save_product_candidates': True,
            'save_session_candidates': True,
            'options_file': 'latest_discovery.json'
        })
    
    def _create_default_config(self):
        """创建默认配置文件（简化版）"""
        default_config = {
            "target_event": {
                "url": "https://shows.cityline.com/sc/2025/example.html"
            },
            "ticket_preferences": {
                "quantity": 2,
                "preferred_zones": ["VIP", "A区", "B区"]
            },
            "purchase_settings": {
                "auto_purchase": False,
                "auto_submit_order": False,
                "max_wait_time": 300
            },
            "browser_config": {
                "headless": False,
                "page_timeout": 30,
                "stealth_mode": True,
                "window_size": [1920, 1080],
                "user_data_dir": "artifacts/chrome-profile",
                "use_subprocess": False,
                "disable_images_in_fast_mode": False,
                "disable_background_networking": False,
                "disable_extensions_in_fast_mode": False,
                "accept_language": "zh-HK,zh-TW,zh-CN,en-US,en"
            },
            "runtime": {
                "diagnostics": True,
                "save_screenshots": True,
                "non_interactive": False,
                "verbose_login_status": False
            },
            "cloudflare": {
                "manual_wait_seconds": 300,
                "check_interval": 1.5,
                "keep_browser_open_on_failure": True,
                "safe_mode": True
            },
            "notifications": {
                "success_message": "🎉 购票成功！门票已预订"
            }
        }
        
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, ensure_ascii=False, indent=2)
        print(f"✅ 已创建默认配置文件: {self.config_path}")
    
    def create_browser(self) -> bool:
        """创建浏览器实例（使用验证成功的兼容配置）"""
        try:
            print("🚀 正在启动浏览器...")
            print("💡 使用兼容性优化的配置")
            
            options = uc.ChromeOptions()
            browser_config = self.config.get('browser_config', {})
            binary_location = browser_config.get('binary_location')
            if binary_location:
                options.binary_location = binary_location
            
            # 基础配置（兼容性优先）
            window_size = browser_config.get('window_size', [1920, 1080])
            options.add_argument(f"--window-size={window_size[0]},{window_size[1]}")
            accept_language = browser_config.get('accept_language')
            if accept_language:
                options.add_argument(f"--lang={str(accept_language).split(',')[0]}")
            user_agent = browser_config.get('user_agent')
            if user_agent:
                options.add_argument(f"--user-agent={user_agent}")
            user_data_dir = browser_config.get('user_data_dir')
            if user_data_dir:
                Path(user_data_dir).mkdir(parents=True, exist_ok=True)
                options.add_argument(f"--user-data-dir={user_data_dir}")

            prefs = {}
            if accept_language:
                prefs["intl.accept_languages"] = accept_language
            if self.config.get('runtime', {}).get('speed_mode') in {'fast', 'turbo'}:
                if browser_config.get('disable_extensions_in_fast_mode', False):
                    options.add_argument("--disable-extensions")
                if browser_config.get('disable_background_networking', False):
                    options.add_argument("--disable-background-networking")
                if browser_config.get('disable_images_in_fast_mode', False):
                    prefs["profile.managed_default_content_settings.images"] = 2
                prefs["profile.default_content_setting_values.notifications"] = 2
                if self.config.get('cloudflare', {}).get('safe_mode', True):
                    print("🛡️ Cloudflare安全模式已启用：保留图片/扩展/后台网络以提高人工验证成功率")
            if prefs:
                options.add_experimental_option("prefs", prefs)
            
            # 优化的反检测设置（仅使用兼容参数）
            if browser_config.get('stealth_mode', True):
                # 只使用确定兼容的参数
                options.add_argument("--disable-blink-features=AutomationControlled")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--no-sandbox")
                # 避免使用会暴露自动化特征的experimental_option
            
            # 使用验证成功的核心参数
            self.driver = uc.Chrome(
                options=options,
                headless=browser_config.get('headless', False),
                use_subprocess=browser_config.get('use_subprocess', False)
            )
            
            # 设置超时
            page_timeout = browser_config.get('page_timeout', 30)
            self.driver.set_page_load_timeout(page_timeout)
            try:
                self.driver.set_script_timeout(max(10, int(page_timeout)))
            except Exception:
                pass
            
            print("✅ 浏览器启动成功（兼容性优化版本）")
            return True
            
        except Exception as e:
            print(f"❌ 浏览器启动失败: {e}")
            self._capture_diagnostics("browser_start_failed", include_html=False)
            print("💡 建议检查Chrome浏览器安装和undetected-chromedriver版本")
            return False
    
    def handle_cloudflare_verification(self, url: str) -> bool:
        """处理Cloudflare验证（基于成功验证的方法）"""
        try:
            print("🛡️ 检查Cloudflare验证...")
            
            # 检测Cloudflare
            has_cloudflare = self._detect_cloudflare()
            
            if not has_cloudflare:
                print("✅ 未检测到Cloudflare验证")
                return True
            
            print("🔒 检测到Cloudflare验证")
            print("💡 请手动完成验证...")
            
            # 高亮验证区域
            self._highlight_verification_areas()
            
            print("👤 请手动完成Cloudflare验证：")
            print("   1. 点击验证复选框")
            print("   2. 等待绿色对勾出现")
            print("   3. 完成任何图像验证")
            
            # 智能等待验证完成
            completed = self._wait_for_verification_complete()
            if completed:
                return True

            print("⚠️ Cloudflare验证未在预期时间内完成")
            self._capture_cloudflare_diagnostics("cloudflare_verification_timeout")
            if self.config.get('cloudflare', {}).get('keep_browser_open_on_failure', True):
                print("💡 浏览器将保持打开，请手动完成验证后重新运行或继续操作")
            return False
            
        except Exception as e:
            print(f"❌ Cloudflare处理异常: {e}")
            return False
    
    def _detect_cloudflare(self) -> bool:
        """检测是否存在Cloudflare验证（智能检测）"""
        try:
            # 等待页面稳定
            time.sleep(float(self.config.get('cloudflare', {}).get('detection_settle_seconds', 0.5) or 0.5))
            
            page_source = self.driver.page_source.lower()
            page_title = self.driver.title.lower()
            current_url = self.driver.current_url.lower()
            
            # 强指示器（确实有Cloudflare）
            strong_indicators = [
                "checking your browser" in page_source,
                "just a moment" in page_source,
                "verify you are human" in page_source,
                "正在验证" in page_source,
                "cf_chl" in page_source,
                "challenge-platform" in page_source,
                "turnstile" in page_source,
                "please wait" in page_title,
                "cloudflare" in page_title,
                len(self.driver.find_elements(By.CSS_SELECTOR, ".cf-turnstile")) > 0,
                len(self.driver.find_elements(By.CSS_SELECTOR, "[data-sitekey]")) > 0,
                len(self.driver.find_elements(By.CSS_SELECTOR, "#cf-challenge")) > 0,
                len(self.driver.find_elements(By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']")) > 0,
                len(self.driver.find_elements(By.CSS_SELECTOR, "iframe[title*='Cloudflare']")) > 0
            ]
            
            # 弱指示器（可能只是引用）
            weak_indicators = [
                "cloudflare" in page_source,
                "ray id" in page_source
            ]
            
            # 排除指示器（明确不是Cloudflare页面）
            exclude_indicators = [
                len(page_source) > 50000,  # 完整页面通常很长
                "cityline" in current_url and len(page_source) > 10000,  # Cityline正常页面
                "login" in current_url and "username" in page_source,  # 登录页面
                "shows.cityline.com" in current_url and len(page_source) > 5000  # 活动页面
            ]
            
            # 如果有强指示器，确认为Cloudflare
            if any(strong_indicators):
                print("🔍 检测到明确的Cloudflare验证页面")
                return True
            
            # 如果有排除指示器，确认不是Cloudflare
            if any(exclude_indicators):
                print("🔍 页面似乎已正常加载，无需Cloudflare验证")
                return False
            
            # 如果只有弱指示器，进一步检查
            if any(weak_indicators):
                print("🔍 检测到可能的Cloudflare元素，等待确认...")
                time.sleep(3)  # 等待可能的重定向
                
                # 重新检查
                new_page_source = self.driver.page_source.lower()
                if any(indicator in new_page_source for indicator in ["checking your browser", "just a moment"]):
                    return True
                
                print("🔍 确认为正常页面，跳过Cloudflare处理")
                return False
            
            return False
            
        except Exception as e:
            print(f"⚠️ Cloudflare检测异常: {e}")
            return False
    
    def _highlight_verification_areas(self):
        """高亮显示验证区域"""
        try:
            highlight_script = """
                var selectors = ['.cf-turnstile', '[data-sitekey]', '#cf-challenge'];
                selectors.forEach(function(selector) {
                    var elements = document.querySelectorAll(selector);
                    elements.forEach(function(el) {
                        if (el && el.offsetHeight > 0) {
                            el.style.border = '3px solid #ff6b6b';
                            el.style.borderRadius = '5px';
                            el.style.backgroundColor = 'rgba(255, 107, 107, 0.1)';
                            el.scrollIntoView({behavior: 'smooth', block: 'center'});
                        }
                    });
                });
            """
            self.driver.execute_script(highlight_script)
            print("✨ 已高亮显示验证区域")
            
        except Exception as e:
            print(f"⚠️ 高亮显示异常: {e}")
    
    def _wait_for_verification_complete(self) -> bool:
        """等待验证完成"""
        print("⏳ 等待验证完成...")
        cloudflare_config = self.config.get('cloudflare', {})
        max_wait = int(cloudflare_config.get('manual_wait_seconds', 300) or 300)
        check_interval = float(cloudflare_config.get('check_interval', 1.5) or 1.5)
        waited = 0.0
        consecutive_completed = 0

        while waited < max_wait:
            time.sleep(check_interval)
            waited += check_interval
            
            try:
                if self._is_cloudflare_completed():
                    consecutive_completed += 1
                    if consecutive_completed >= 2:
                        print("✅ 验证完成！")
                        self._remove_highlights()
                        return True
                else:
                    consecutive_completed = 0

                remaining = int(max_wait - waited)
                if remaining > 0 and int(waited) % 15 == 0:
                    print(f"⏱️ 等待Cloudflare验证... 剩余约{remaining}秒，当前URL: {self.driver.current_url}")
            except Exception:
                continue
        
        print("⏰ 验证等待超时")
        return False

    def _is_cloudflare_completed(self) -> bool:
        """Return true only when challenge indicators are gone and page is usable."""
        try:
            page_source = self.driver.page_source.lower()
            page_title = self.driver.title.lower()
            current_url = self.driver.current_url.lower()
        except Exception:
            return False

        active_challenge = any([
            "checking your browser" in page_source,
            "just a moment" in page_source,
            "verify you are human" in page_source,
            "cf_chl" in page_source,
            "challenge-platform" in page_source,
            "turnstile" in page_source and len(page_source) < 80000,
            "cloudflare" in page_title,
            len(self.driver.find_elements(By.CSS_SELECTOR, ".cf-turnstile")) > 0,
            len(self.driver.find_elements(By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']")) > 0,
            len(self.driver.find_elements(By.CSS_SELECTOR, "#cf-challenge")) > 0
        ])
        if active_challenge:
            return False

        has_cityline_page = "cityline" in current_url and len(page_source) > 5000
        has_login_ui = any(marker in page_source for marker in [
            "login", "登入", "登錄", "登录", "會員登入", "member login", "username", "password"
        ])
        has_event_page = "shows.cityline.com" in current_url and "login" not in current_url
        usable_page = has_cityline_page and (has_login_ui or has_event_page)
        return bool(usable_page)
    
    def _remove_highlights(self):
        """移除高亮效果"""
        try:
            remove_script = """
                var elements = document.querySelectorAll('[style*="rgb(255, 107, 107)"]');
                elements.forEach(function(el) {
                    el.style.border = '';
                    el.style.backgroundColor = '';
                    el.style.boxShadow = '';
                });
            """
            self.driver.execute_script(remove_script)
        except:
            pass
    
    def _check_login_status(self) -> bool:
        """检查当前登录状态（简化版）"""
        verbose_login = self.config.get('runtime', {}).get('verbose_login_status', False)
        try:
            # 简化获取页面信息，避免连接错误
            try:
                current_url = self.driver.current_url.lower()
                page_source = self.driver.page_source.lower()
                page_title = self.driver.title.lower()
            except:
                # 如果获取页面信息失败，返回未登录
                if verbose_login:
                    print("🔍 检查登录状态: 浏览器连接异常，默认未登录")
                return False

            if verbose_login:
                print(f"🔍 检查登录状态:")
                print(f"   当前URL: {current_url}")
                print(f"   页面标题: {page_title}")
            
            # 1. 明确的未登录指示器（优先级最高）
            not_logged_in_indicators = [
                "login.html" in current_url,  # 在登录页面
                "login" in current_url and "targeturl" in current_url,  # 带重定向的登录页面
                "/login" in current_url,  # 登录路径
                "www.cityline.com/login" in current_url,  # Cityline登录页面
                "请登录" in page_source,
                "please login" in page_source,
                "sign in" in page_title,
                "登录" in page_title and "会员" not in page_source,
                # 特别检查Cityline登录页面特征
                "會員登入" in page_title,  # Cityline登录页面标题特征
                "cityline" in current_url and "login" in current_url
            ]
            
            # 2. 明确的已登录指示器
            logged_in_indicators = [
                "會員" in page_source, "会员" in page_source, "member" in page_source,
                "登出" in page_source, "logout" in page_source, "sign out" in page_source,
                "我的账户" in page_source, "个人中心" in page_source,
                "用户名" in page_source, "username" in page_source
            ]
            
            # 3. 页面内容指示器（活动页面）
            activity_page_indicators = [
                "shows.cityline.com" in current_url and "login" not in current_url,
                "演唱会" in page_source, "concert" in page_source,
                "购票" in page_source, "ticket" in page_source,
                len(page_source) > 10000  # 完整页面通常较长
            ]
            
            # 统计各种指示器
            not_logged_score = sum(not_logged_in_indicators)
            login_score = sum(logged_in_indicators)
            activity_score = sum(activity_page_indicators)
            
            if verbose_login:
                print(f"   📊 未登录指示器得分: {not_logged_score}")
                print(f"   📊 已登录指示器得分: {login_score}")
                print(f"   📊 活动页面指示器得分: {activity_score}")
            
            # 1. 最高优先级：检查明确的未登录状态
            if not_logged_score >= 1:
                if verbose_login:
                    print("   🔐 状态: 未登录（检测到登录页面特征）")
                return False
            
            # 2. 第二优先级：检查明确的已登录状态
            if login_score >= 1 and not_logged_score == 0:
                if verbose_login:
                    print("   ✅ 状态: 已登录（检测到登录指示器）")
                return True
            
            # 3. 第三优先级：检查是否在活动页面
            if activity_score >= 2 and not_logged_score == 0 and "login" not in current_url:
                if verbose_login:
                    print("   ✅ 状态: 已登录（在活动页面）")
                return True
            
            # 4. 默认：状态不明确，倾向于未登录
            if verbose_login:
                print("   ⚠️ 状态: 登录状态不明确，默认为未登录")
            return False
                
        except Exception as e:
            if verbose_login:
                print(f"   ❌ 登录状态检查异常: {e}")
            return False
    
    def _wait_for_login_completion(self, max_wait_time: int = 300) -> bool:
        """智能等待登录完成"""
        try:
            print(f"⏳ 等待用户完成登录（最多等待{max_wait_time}秒）...")
            print("💡 请在浏览器中:")
            print("   1. 选择登录方式（Facebook、Google等）")
            print("   2. 完成登录验证")
            print("   3. 系统检测到登录成功后自动继续")
            print()
            
            check_interval = 5  # 每5秒检查一次
            waited_time = 0
            last_cloudflare_check = -30
            cloudflare_prompted = False
            
            while waited_time < max_wait_time:
                try:
                    current_url = self.driver.current_url
                except Exception as e:
                    print(f"⚠️ 浏览器暂时无响应，继续等待恢复: {str(e).splitlines()[0]}")
                    time.sleep(check_interval)
                    waited_time += check_interval
                    continue

                if waited_time - last_cloudflare_check >= 15:
                    last_cloudflare_check = waited_time
                    try:
                        if self._detect_cloudflare():
                            if not cloudflare_prompted:
                                print("🔒 登录等待期间检测到Cloudflare验证，切换到人工验证等待")
                                cloudflare_prompted = True
                            if not self.handle_cloudflare_verification(current_url):
                                print("❌ 登录页Cloudflare验证未完成")
                                return False
                            print("✅ Cloudflare验证完成，继续等待登录")
                            current_url = self.driver.current_url
                    except Exception as e:
                        print(f"⚠️ Cloudflare检测暂时失败，继续等待: {str(e).splitlines()[0]}")

                # 检查登录状态
                try:
                    if self._check_login_status():
                        print("🎉 检测到登录成功！")
                        return True
                except Exception as e:
                    print(f"⚠️ 登录状态检查暂时失败，继续等待: {str(e).splitlines()[0]}")
                 
                # 检查是否已跳转到活动页面
                if ("shows.cityline.com" in current_url and 
                    "login" not in current_url.lower()):
                    print("🎉 检测到已跳转到活动页面！")
                    return True
                
                # 显示等待状态
                remaining = max_wait_time - waited_time
                if waited_time % 30 == 0 and waited_time > 0:  # 每30秒提示一次
                    print(f"⏱️ 继续等待登录... 剩余{remaining}秒")
                    print(f"   当前页面: {current_url}")
                
                time.sleep(check_interval)
                waited_time += check_interval
            
            print("⏰ 等待登录超时")
            print("💡 您可以:")
            print("   1. 继续手动完成登录")
            print("   2. 重新运行脚本")
            
            return False
            
        except Exception as e:
            print(f"⚠️ 等待登录过程遇到异常，保存诊断后继续保持浏览器: {str(e).splitlines()[0]}")
            self._capture_cloudflare_diagnostics("login_wait_exception")
            return False
    
    def login_member(self) -> bool:
        """会员登录（智能检测和跳过机制）"""
        try:
            member_config = self.config.get('member_info', {})
            
            if not member_config.get('auto_login', False):
                print("⚠️ 自动登录已禁用，跳过登录")
                return True
            
            username = member_config.get('username', '')
            password = member_config.get('password', '')
            
            if not username or not password:
                print("⚠️ 未配置登录信息，跳过自动登录")
                return True
            
            # 先检查当前登录状态
            current_status = self._check_login_status()
            if current_status:
                print("✅ 检测到已登录状态，跳过登录")
                return True
            
            # 如果在登录页面，等待用户完成登录
            current_url = self.driver.current_url.lower()
            if "login.html" in current_url or ("login" in current_url and "targeturl" in current_url):
                print("🔐 检测到正在登录页面，等待用户完成登录...")
                if self._detect_cloudflare():
                    if not self.handle_cloudflare_verification(self.driver.current_url):
                        print("❌ 登录页Cloudflare验证未完成")
                        return False
                return self._wait_for_login_completion()
            
            print("🔐 开始会员登录...")
            
            # 访问登录页面
            try:
                self.driver.get("https://www.cityline.com/member/login")
                self._wait_for_page_ready(timeout=10)
            except Exception as e:
                print(f"⚠️ 访问登录页面失败: {e}")
                print("💡 跳过登录，直接尝试购票")
                return True
            
            # 处理可能的Cloudflare
            if not self.handle_cloudflare_verification(self.driver.current_url):
                print("❌ 登录页面Cloudflare处理失败")
                return False
            
            # 登录操作
            wait = WebDriverWait(self.driver, 10)
            
            try:
                # 尝试多种可能的用户名输入框定位方式
                username_selectors = [
                    (By.NAME, "username"),
                    (By.NAME, "loginName"),
                    (By.ID, "username"),
                    (By.ID, "loginName"),
                    (By.CSS_SELECTOR, "input[type='text']"),
                    (By.CSS_SELECTOR, "input[placeholder*='用户名']"),
                    (By.CSS_SELECTOR, "input[placeholder*='电话']")
                ]
                
                username_field = None
                for selector in username_selectors:
                    try:
                        username_field = wait.until(EC.presence_of_element_located(selector))
                        if username_field.is_displayed():
                            break
                    except:
                        continue
                
                if not username_field:
                    print("⚠️ 未找到用户名输入框，可能需要手动登录")
                    self._capture_diagnostics("login_username_missing")
                    return False
                
                username_field.clear()
                username_field.send_keys(username)
                
                time.sleep(random.uniform(1, 2))
                
                password_field = self.driver.find_element(By.NAME, "password")
                password_field.clear()
                password_field.send_keys(password)
                
                time.sleep(random.uniform(1, 2))
                
                # 提交登录
                login_button = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
                login_button.click()
                
                time.sleep(1)  # 减少登录等待
                
                # 验证登录状态
                if "login" not in self.driver.current_url.lower():
                    print("✅ 登录成功！")
                    return True
                else:
                    print("⚠️ 登录状态不确定，请检查")
                    return False
                    
            except Exception as e:
                print(f"⚠️ 登录操作异常: {e}")
                self._capture_diagnostics("login_failed")
                return False
                
        except Exception as e:
            print(f"❌ 登录过程异常: {e}")
            return False
    
    def access_event_page(self) -> bool:
        """访问活动页面（增强连接稳定性）"""
        try:
            event_config = self.config.get('target_event', {})
            event_urls = event_config.get('urls') or [event_config.get('url', '')]
            event_urls = [url for url in event_urls if url]
            
            if not event_urls:
                print("❌ 未配置活动URL")
                return False

            print(f"🎯 访问活动页面: {event_urls[0]}")
            
            # 重试机制
            max_retries = 3
            for attempt in range(max_retries):
                event_url = event_urls[min(attempt, len(event_urls) - 1)]
                try:
                    if attempt > 0:
                        print(f"🔁 第{attempt + 1}次尝试访问: {event_url}")
                    self.driver.get(event_url)
                    self._wait_for_page_ready(timeout=browser_config.get('page_timeout', 30) if (browser_config := self.config.get('browser_config', {})) else 30)
                    self._mark_metric('event_page_ready')
                    
                    # 验证页面加载成功
                    if len(self.driver.page_source) < 1000:
                        print(f"⚠️ 页面加载异常，第{attempt+1}次重试...")
                        self._wait_until(lambda: len(self.driver.page_source) >= 1000, timeout=2, label="页面源码长度")
                        continue
                    
                    
                    # 检查是否被重定向到登录页面
                    current_url = self.driver.current_url.lower()
                    if ("login.html" in current_url or 
                        ("login" in current_url and "targeturl" in current_url)):
                        print("🔐 检测到被重定向到登录页面")
                        print("💡 需要用户手动登录后才能访问活动页面")
                        
                        # 等待用户完成登录
                        if self._wait_for_login_completion():
                            print("✅ 登录完成，返回目标活动页面继续购票")
                            current_after_login = self.driver.current_url.lower()
                            if "shows.cityline.com" not in current_after_login or "login" in current_after_login:
                                self.driver.get(event_url)
                                self._wait_for_page_ready(timeout=browser_config.get('page_timeout', 30) if (browser_config := self.config.get('browser_config', {})) else 30)
                        else:
                            print("⚠️ 登录等待未确认成功，尝试重新访问目标活动页面验证登录状态")
                            try:
                                self.driver.get(event_url)
                                self._wait_for_page_ready(timeout=browser_config.get('page_timeout', 30) if (browser_config := self.config.get('browser_config', {})) else 30)
                                verify_url = self.driver.current_url.lower()
                                if "login" in verify_url and "targeturl" in verify_url:
                                    print("❌ 重新访问后仍被重定向到登录页")
                                    return False
                                print("✅ 重新访问活动页成功，继续购票流程")
                            except Exception as verify_error:
                                print(f"❌ 登录后重访活动页面失败: {verify_error}")
                                return False

                    print("✅ 成功访问活动页面")
                    if not self._prepare_target_product():
                        return False
                    self._mark_metric('product_strategy_complete')
                    sale_state = self._wait_for_sale_state_if_needed()
                    if sale_state == 'stop':
                        return False
                    self._mark_metric('sale_state_ready')
                    # 继续执行点击"前往购票"的流程
                    return self._execute_purchase_button_flow()
                    
                except Exception as e:
                    print(f"⚠️ 第{attempt+1}次访问失败: {e}")
                    if attempt < max_retries - 1:
                        self._wait_until(lambda: False, timeout=3, interval=0.5, label="重试间隔")
                        continue
                    else:
                        raise e
            
            return False
            
        except Exception as e:
            print(f"❌ 访问活动页面失败: {e}")
            self._capture_diagnostics("access_event_page_failed")
            print("💡 建议检查网络连接和活动URL有效性")
            return False

    def _prepare_target_product(self) -> bool:
        """Apply product strategy before clicking purchase."""
        try:
            strategy = self.config.get('target_event', {}).get('product_strategy', {})
            mode = strategy.get('mode', 'current_url')
            if mode in {'current_url', 'disabled', 'none'}:
                return True

            print("🧠 执行智能商品识别...")
            page_text = f"{self.driver.title}\n{self.driver.current_url}\n{self.driver.page_source[:80000]}"
            avoid_keywords = strategy.get('avoid_keywords', [])
            if self._contains_any_keyword(page_text, avoid_keywords):
                print("⛔ 当前页面命中商品排除词，停止继续")
                self._capture_diagnostics("product_avoid_keyword")
                return False

            title_score = self._keywords_score(page_text, strategy.get('title_keywords', []), default_weight=30)
            date_score = self._keywords_score(page_text, strategy.get('date_keywords', []), default_weight=10)
            if title_score > 0 or strategy.get('fallback_to_current_url', True):
                print(f"✅ 当前页面商品匹配得分: {title_score + date_score}")
                return True

            candidates = self._scan_product_candidates(strategy)
            if not candidates:
                print("⚠️ 未扫描到商品候选")
                return bool(strategy.get('fallback_to_current_url', True))

            best = candidates[0]
            if best['score'] <= 0 and not strategy.get('fallback_to_current_url', True):
                print("❌ 未找到符合策略的商品")
                self._capture_diagnostics("product_not_matched")
                return False

            if strategy.get('click_best_match', True) and best.get('element'):
                print(f"🎯 点击最佳商品: {best['text'][:80]} (得分 {best['score']})")
                return self._safe_click(best['element'], "目标商品") and self._wait_for_page_ready(timeout=10)

            return True
        except Exception as e:
            print(f"⚠️ 智能商品识别异常: {e}")
            self._capture_diagnostics("product_strategy_failed")
            return True

    def _scan_product_candidates(self, strategy: Dict) -> List[Dict]:
        """Scan visible cards/links and rank possible products."""
        selectors = strategy.get('candidate_selectors') or [
            'a[href]', '[onclick]', '.event-card', '.show-card', '.event-item', '.product-item', 'article'
        ]
        candidates = []
        seen = set()
        for selector in selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    try:
                        if not element.is_displayed():
                            continue
                        text = element.text.strip()
                        href = element.get_attribute('href') or ''
                        onclick = element.get_attribute('onclick') or ''
                        combined = f"{text} {href} {onclick}".strip()
                        key = combined[:300]
                        if not combined or key in seen:
                            continue
                        seen.add(key)
                        if self._contains_any_keyword(combined, strategy.get('avoid_keywords', [])):
                            score = -9999
                        else:
                            score = 0
                            score += self._keywords_score(combined, strategy.get('title_keywords', []), default_weight=30)
                            score += self._keywords_score(combined, strategy.get('date_keywords', []), default_weight=10)
                            if href:
                                score += 5
                        candidates.append({'text': combined, 'score': score, 'href': href, 'selector': selector, 'element': element})
                    except Exception:
                        continue
            except Exception:
                continue

        candidates.sort(key=lambda item: item['score'], reverse=True)
        serializable = [{k: v for k, v in item.items() if k != 'element'} for item in candidates[:30]]
        if self.config.get('target_event', {}).get('discovery_mode', {}).get('save_product_candidates', True):
            self._save_discovery_result('latest_product_candidates', {'products': serializable})
        if candidates:
            print("📊 商品候选Top 5:")
            for item in candidates[:5]:
                print(f"   得分 {item['score']}: {item['text'][:80]}")
        return candidates

    def _select_preferred_session(self) -> bool:
        """Select preferred date/session on ticket page when available."""
        try:
            strategy = self.config.get('target_event', {}).get('session_strategy', {})
            mode = strategy.get('mode', 'first_available')
            if mode in {'disabled', 'none'}:
                return True

            candidates = self._scan_session_candidates(strategy)
            if not candidates:
                print("ℹ️ 未发现可选择的场次/日期")
                return True

            best = candidates[0]
            if best['score'] <= -999:
                print("⚠️ 场次候选均被排除，跳过场次选择")
                return bool(strategy.get('allow_any_session_if_preferred_unavailable', True))

            if best.get('selected'):
                print(f"✅ 当前场次已符合策略: {best['text'][:60]}")
                return True

            if best.get('element'):
                print(f"📅 选择最佳场次: {best['text'][:80]} (得分 {best['score']})")
                clicked = self._safe_click(best['element'], "目标场次")
                if clicked:
                    self._wait_for_page_ready(timeout=5)
                    time.sleep(0.3)
                return clicked
            return True
        except Exception as e:
            print(f"⚠️ 智能场次选择异常: {e}")
            self._capture_diagnostics("session_strategy_failed")
            return True

    def _scan_session_candidates(self, strategy: Dict) -> List[Dict]:
        """Scan visible date/session controls and rank them."""
        selectors = strategy.get('candidate_selectors') or [
            '.date-box', '[class*="date"]', '[class*="session"]', '[class*="time"]',
            'button[class*="date"]', 'button[class*="time"]', 'button', 'a[role="button"]'
        ]
        candidates = []
        seen = set()
        preferred = strategy.get('preferred', [])
        preferred_dates = strategy.get('preferred_dates', [])
        preferred_times = strategy.get('preferred_times', [])
        avoid_dates = strategy.get('avoid_dates', [])

        for selector in selectors:
            try:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if not element.is_displayed() or not element.is_enabled():
                            continue
                        text = element.text.strip()
                        aria = element.get_attribute('aria-label') or ''
                        classes = element.get_attribute('class') or ''
                        combined = f"{text} {aria} {classes}".strip()
                        if not combined or combined in seen:
                            continue
                        seen.add(combined)
                        if not self._looks_like_session_text(combined):
                            continue

                        score = 10
                        if self._contains_any_keyword(combined, avoid_dates):
                            score = -9999
                        score += self._keywords_score(combined, preferred, default_weight=40)
                        score += self._keywords_score(combined, preferred_dates, default_weight=30)
                        score += self._keywords_score(combined, preferred_times, default_weight=20)
                        if 'active' in classes.lower() or 'selected' in classes.lower():
                            score += 15
                        if self._contains_any_keyword(combined, ['售罄', 'sold out', 'unavailable']):
                            score -= 500

                        candidates.append({
                            'text': combined,
                            'score': score,
                            'selector': selector,
                            'selected': 'active' in classes.lower() or 'selected' in classes.lower(),
                            'element': element
                        })
                    except Exception:
                        continue
            except Exception:
                continue

        if not candidates and strategy.get('fallback') == 'first_available':
            return []

        if preferred or preferred_dates or preferred_times:
            candidates.sort(key=lambda item: item['score'], reverse=True)
        else:
            candidates.sort(key=lambda item: (item['score'], -len(item['text'])), reverse=True)

        serializable = [{k: v for k, v in item.items() if k != 'element'} for item in candidates[:30]]
        if self.config.get('target_event', {}).get('discovery_mode', {}).get('save_session_candidates', True):
            self._save_discovery_result('latest_session_candidates', {'sessions': serializable})
        if candidates:
            print("📊 场次候选Top 5:")
            for item in candidates[:5]:
                print(f"   得分 {item['score']}: {item['text'][:80]}")
        return candidates

    def _looks_like_session_text(self, text: str) -> bool:
        """Heuristic for date/time/session candidates."""
        normalized = self._normalize_text(text)
        patterns = [
            r'\b20\d{2}[-/.年]\d{1,2}',
            r'\b\d{1,2}[:：]\d{2}\b',
            r'\b\d{1,2}\s*(am|pm)\b',
            r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)',
            r'(一月|二月|三月|四月|五月|六月|七月|八月|九月|十月|十一月|十二月)',
            r'(場次|场次|日期|date|session|time)'
        ]
        return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns)

    def _wait_for_sale_state_if_needed(self) -> str:
        """Poll page until sale state is clickable/stop/timeout according to strategy."""
        strategy = self.config.get('target_event', {}).get('sale_state_strategy', {})
        if not strategy.get('detect_states', True):
            return 'unknown'

        max_poll = int(strategy.get('max_poll_seconds', 120) or 120)
        poll_until = _parse_bool(strategy.get('poll_until_on_sale'), False)
        start_time = time.time()
        last_state = None

        while True:
            state = self._detect_sale_state()
            if state != last_state:
                print(f"🔎 售卖状态: {state}")
                last_state = state
            if state in {'clickable', 'unknown'}:
                return state
            if state == 'stop':
                print("⛔ 检测到售罄/取消等停止状态")
                self._capture_diagnostics("sale_state_stop")
                return state
            if state == 'refresh':
                print("🔄 检测到繁忙状态，刷新重试")
                self.driver.refresh()
                self._wait_for_page_ready(timeout=10)

            if not poll_until or time.time() - start_time >= max_poll:
                return state

            interval = float(strategy.get('pre_start_poll_interval', 1.5))
            if time.time() - start_time > max_poll * 0.8:
                interval = float(strategy.get('on_start_poll_interval', 0.3))
            time.sleep(max(interval, 0.1))
            try:
                self.driver.refresh()
                self._wait_for_page_ready(timeout=10)
            except Exception:
                pass
    
    def _execute_purchase_button_flow(self) -> bool:
        """执行点击'前往购票'按钮和后续流程"""
        try:
            print("🎯 开始执行购票按钮流程...")
            post_click_wait = float(self._speed_value('post_click_wait', 0.25))
            
            # 直接寻找并点击"前往购票"按钮
            print("🔍 直接寻找'前往购票'按钮...")
            
            target_button = None
            original_url = self.driver.current_url
            original_windows = self.driver.window_handles
            
            # 方法1：使用持续快速查找（预编译选择器）
            print("🔄 持续搜索go按钮...")
            target_button = self._fast_find_button('go_buttons', max_wait=float(self._speed_value('button_search_timeout', 6)))
            
            if not target_button:
                # 方法2：使用文本匹配查找购票按钮（持续搜索）
                print("🔄 持续搜索购票按钮...")
                target_button = self._fast_find_button('purchase_btns', ['前往購票', '前往购票', '立即購買', '立即购买', '快速購票', '快速购票'], max_wait=float(self._speed_value('button_search_timeout', 6)))
            
            if not target_button:
                # 方法3：传统选择器查找（备用）
                button_selectors = [
                    "button[onclick*='goevent'], button[onclick*='goEvent'], a[onclick*='goevent'], a[onclick*='goEvent']",
                    "button, a[role='button']"
                ]
                
                for selector in button_selectors:
                    try:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        for element in elements:
                            if element.is_displayed() and element.is_enabled():
                                text = element.text.strip()
                                normalized_text = self._normalize_whitespace(text) if text else ''
                            
                            # 检查是否是"前往购票"相关按钮
                            if normalized_text:  # 如果有文本（空白歸一化後）
                                # 检查是否包含购票关键词（支持简体和繁体）
                                if ('前' in normalized_text and '往' in normalized_text and ('购' in normalized_text or '購' in normalized_text) and '票' in normalized_text) or \
                                   '前往購票' in normalized_text or '前往购票' in normalized_text or \
                                   '立即購買' in normalized_text or '立即购买' in normalized_text or \
                                   '馬上購買' in normalized_text or '马上购买' in normalized_text or \
                                   '快速購票' in normalized_text or '快速购票' in normalized_text or \
                                   'buy ticket' in normalized_text.lower() or \
                                   'purchase' in normalized_text.lower():
                                    
                                    # 排除第三方登录按钮
                                    if not any(exclude in normalized_text.lower() for exclude in ['facebook', 'google', 'login', '登录', '微信', 'wechat']):
                                        target_button = element
                                        print(f"✅ 找到目标按钮: '{text}' (选择器: {selector})")
                                        break
                            
                            # 检查 onclick 属性
                            onclick = element.get_attribute('onclick') or ''
                            onclick_lower = onclick.lower()
                            if any(gk in onclick_lower for gk in ['gopurchase', 'go_event', 'goevent', 'go_purchase']):
                                target_button = element
                                print(f"✅ 找到目标按钮 (onclick匹配: {selector})")
                                break
                            
                            elif selector in ["#buyTicketBtn", ".load-button", "button[onclick*='go()']"]:
                                # 对于特定的选择器，即使没有文本也接受
                                target_button = element
                                print(f"✅ 找到目标按钮 (选择器: {selector})")
                                break
                        
                        if target_button:
                            break
                            
                    except Exception as e:
                        continue
            
            if not target_button:
                print("❌ 未找到'前往购票'按钮，显示调试信息...")
                self._show_debug_buttons()
                self._capture_diagnostics("purchase_button_not_found")
                return False
            self._mark_metric('purchase_button_found')
            
            # 高亮并点击按钮
            self.driver.execute_script("""
                arguments[0].style.border = '3px solid #00ff00';
                arguments[0].style.backgroundColor = 'rgba(0,255,0,0.2)';
                arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});
            """, target_button)
            
            print("🖱️ 点击'前往购票'按钮...")
            self._safe_click(target_button, "前往购票按钮")
            self._mark_metric('purchase_button_clicked')
            time.sleep(post_click_wait)
            
            # 检查是否有新窗口
            new_windows = self.driver.window_handles
            if len(new_windows) > len(original_windows):
                print("🔄 检测到新窗口，切换中...")
                self.driver.switch_to.window(new_windows[-1])
                time.sleep(post_click_wait)
                
            new_url = self.driver.current_url
            print(f"🌐 当前URL: {new_url}")
            
            # 处理venue页面的继续流程
            if "venue.cityline.com" in new_url:
                print("🏟️ 进入venue页面，开始处理继续流程...")
                return self._handle_venue_continue_flow()
            else:
                print("✅ 购票按钮点击成功")
                return True
                
        except Exception as e:
            print(f"❌ 购票按钮流程异常: {e}")
            self._capture_diagnostics("purchase_button_flow_failed")
            return False
    

    

    
    def _is_seat_selection_page(self) -> bool:
        """判断是否为选座页面"""
        try:
            seat_indicators = [
                ".seat", ".座位", "seat-map", "选座",
                "venue.cityline.com" in self.driver.current_url,
                "座位图" in self.driver.page_source
            ]
            
            return any([
                self.driver.find_elements(By.CSS_SELECTOR, ".seat"),
                "选座" in self.driver.page_source,
                "seat" in self.driver.page_source.lower(),
                "venue.cityline.com" in self.driver.current_url
            ])
            
        except:
            return False
    
    def handle_seat_selection(self) -> bool:
        """处理选座流程"""
        try:
            print("💺 开始选座流程...")
            
            # 等待选座页面加载
            time.sleep(5)
            
            # 检查是否在venue页面
            if "venue.cityline.com" in self.driver.current_url:
                print("🏟️ 检测到venue页面，等待加载...")
                
                # 处理可能的Cloudflare
                if not self.handle_cloudflare_verification(self.driver.current_url):
                    print("❌ venue页面Cloudflare处理失败")
                    return False
                
                # 等待并寻找继续按钮
                return self._handle_venue_page()
            
            # 如果不是venue页面，寻找其他选座元素
            print("🔍 寻找选座选项...")
            
            return True
            
        except Exception as e:
            print(f"❌ 选座流程异常: {e}")
            return False
    
    def _handle_venue_continue_flow(self) -> bool:
        """处理venue页面的继续流程（参考项目风格）"""
        try:
            print("🏟️ 处理venue页面继续流程...")
            
            # 等待页面完全加载
            time.sleep(2)
            
            # venue页面不需要Cloudflare验证，直接处理按钮
            
            # 参考项目的继续按钮策略（更精确）
            continue_strategies = [
                # 策略1: onclick事件（最优先）
                {"selector": "button[onclick*='goEvent']", "method": "onclick_goEvent", "priority": 100},
                {"selector": "button[onclick*='goevent']", "method": "onclick_goevent", "priority": 95},
                {"selector": "a[onclick*='goEvent']", "method": "link_goEvent", "priority": 90},
                
                # 策略2: 特定class（参考项目常用）
                {"selector": ".btn_cta", "method": "btn_cta_class", "priority": 85},
                {"selector": ".queue-button", "method": "queue_button_class", "priority": 80},
                {"selector": ".continue-btn", "method": "continue_btn_class", "priority": 75},
                
                # 策略3: 文本匹配（优先繁体中文）
                {"selector": "//button[contains(text(), '繼續')]", "method": "xpath_continue_tc", "priority": 85},      # 繁体继续 - 最高
                {"selector": "//button[contains(text(), '登入')]", "method": "xpath_login_universal", "priority": 82},  # 通用登入
                {"selector": "//button[contains(text(), '登錄')]", "method": "xpath_login_tc", "priority": 80},         # 繁体登录
                {"selector": "//a[contains(text(), '繼續')]", "method": "xpath_link_continue_tc", "priority": 78},      # 繁体继续链接
                {"selector": "//a[contains(text(), '登入')]", "method": "xpath_login_link", "priority": 75},            # 通用登入链接
                {"selector": "//button[contains(text(), '继续')]", "method": "xpath_continue_zh", "priority": 70},      # 简体继续
                {"selector": "//button[contains(text(), '登录')]", "method": "xpath_login_zh", "priority": 68},         # 简体登录
                {"selector": "//button[contains(text(), 'Continue')]", "method": "xpath_continue_en", "priority": 65}, # 英文继续
                {"selector": "//button[contains(text(), '排隊')]", "method": "xpath_queue_tc", "priority": 62},         # 繁体排队
                {"selector": "//button[contains(text(), '排队')]", "method": "xpath_queue", "priority": 60},            # 简体排队
                {"selector": "//a[contains(text(), '继续')]", "method": "xpath_link_continue", "priority": 55},         # 简体继续链接
                {"selector": "//button[contains(text(), 'Login')]", "method": "xpath_login_en", "priority": 50}        # 英文登录
            ]
            
            # 按优先级排序
            continue_strategies.sort(key=lambda x: x["priority"], reverse=True)
            
            found_buttons = []
            
            # 直接使用已知有效的按钮选择器
            
            # 收集所有可能的继续按钮
            for strategy in continue_strategies:
                try:
                    selector = strategy["selector"]
                    method = strategy["method"]
                    priority = strategy["priority"]
                    
                    if selector.startswith("//"):
                        elements = self.driver.find_elements(By.XPATH, selector)
                    else:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            text = element.text.strip()
                            found_buttons.append({
                                'element': element,
                                'text': text,
                                'method': method,
                                'priority': priority,
                                'selector': selector
                            })
                            
                except Exception as e:
                    print(f"   策略 {method} 失败: {e}")
                    continue
            
            if found_buttons:
                # 按优先级排序
                found_buttons.sort(key=lambda x: x["priority"], reverse=True)
                
                # 选择最高优先级的按钮
                selected_button = found_buttons[0]
                element = selected_button['element']
                
                print(f"✅ 找到继续按钮: '{selected_button['text']}'")
                
                # 使用JavaScript点击，避免被遮挡
                try:
                    # 先滚动到按钮位置
                    self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
                    time.sleep(0.5)
                    
                    # 使用JavaScript点击
                    self.driver.execute_script("arguments[0].click();", element)
                    time.sleep(1)
                    print("✅ 继续按钮点击完成 (JavaScript点击)")
                    
                    # 等待并寻找登入按钮（智能等待）
                    print("🔍 寻找登入按钮...")
                    login_found = self._smart_wait_for_login_button()
                    
                    if not login_found:
                        print("⚠️ 未找到登入按钮，检查是否已自动跳转...")
                        # 等待可能的自动跳转
                        time.sleep(2)
                        current_url = self.driver.current_url
                        print(f"🌐 检查跳转后URL: {current_url}")
                        
                        # 检查多种可能的页面状态
                        if "performance" in current_url:
                            print("🎯 检测到购票页面，开始自动购票...")
                            purchase_result = self.complete_purchase_flow()
                            if purchase_result.success:
                                print(f"🎉 {purchase_result.message}")
                            else:
                                print(f"⚠️ {purchase_result.message}")
                        elif "eventDetail" in current_url:
                            print("🔍 仍在活动详情页面，尝试寻找更多按钮...")
                            # 扩展搜索范围，寻找任何可能的按钮
                            self._find_and_click_any_purchase_button()
                        else:
                            print(f"⚠️ 未识别的页面类型: {current_url}")
                            print("💡 可能需要手动操作或检查页面状态")
                    
                    return True
                    
                except Exception as e:
                    print(f"❌ 点击继续按钮失败: {e}")
                    return False
            else:
                print("❌ 未找到继续按钮，显示调试信息...")
                # 只有找不到按钮时才显示调试信息
                self._show_debug_buttons()
                return False
            
        except Exception as e:
            print(f"❌ venue继续流程异常: {e}")
            return False
    
    def _show_debug_buttons(self):
        """显示调试信息：页面上所有按钮"""
        try:
            print("🔍 调试：扫描页面上所有按钮...")
            all_buttons = []
            
            button_selectors = [
                "button", "a", "input[type='button']", "input[type='submit']", 
                ".btn", "[role='button']", "[onclick]"
            ]
            
            for selector in button_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            text = element.text.strip()
                            onclick = element.get_attribute('onclick') or ''
                            class_name = element.get_attribute('class') or ''
                            
                            if text or onclick:
                                all_buttons.append({
                                    'text': text,
                                    'onclick': onclick,
                                    'class': class_name
                                })
                                
                except Exception as e:
                    continue
            
            print(f"📊 发现 {len(all_buttons)} 个按钮:")
            for i, btn in enumerate(all_buttons[:10], 1):  # 只显示前10个
                text_preview = btn['text'][:50] + "..." if len(btn['text']) > 50 else btn['text']
                print(f"   {i}. 文本: '{text_preview}'")
                if btn['onclick']:
                    onclick_preview = btn['onclick'][:50] + "..." if len(btn['onclick']) > 50 else btn['onclick']
                    print(f"      onclick: {onclick_preview}")
                if btn['class']:
                    print(f"      class: {btn['class'][:50]}")
                print()
                
        except Exception as e:
            print(f"⚠️ 调试信息获取失败: {e}")
    
    def _wait_for_cloudflare_and_login_button(self) -> bool:
        """等待Cloudflare验证完成并点击登入按钮"""
        try:
            print("⏳ 等待Cloudflare验证和登入按钮...")
            max_wait = 60
            check_interval = 3
            
            for i in range(0, max_wait, check_interval):
                time.sleep(check_interval)
                
                # 检查是否有Cloudflare验证
                try:
                    page_source = self.driver.page_source.lower()
                    
                    # 检查是否有welcome窗口或验证完成
                    if "welcome" in page_source or "cloudflare" in page_source:
                        print(f"🔍 检测到验证页面，继续等待... ({i+check_interval}秒)")
                        
                        # 处理Cloudflare验证
                        if not self.handle_cloudflare_verification(self.driver.current_url):
                            print("⚠️ Cloudflare处理失败，继续尝试...")
                        
                        continue
                    
                    # 寻找登入按钮（优先繁体中文）
                    login_button_selectors = [
                        "//button[contains(text(), '登入')]",        # 通用登入
                        "//button[contains(text(), '登錄')]",        # 繁体登录
                        "//button[contains(text(), '登录')]",        # 简体登录
                        "//a[contains(text(), '登入')]",            # 通用登入链接
                        "//a[contains(text(), '登錄')]",            # 繁体登录链接
                        "//a[contains(text(), '登录')]",            # 简体登录链接
                        "//button[contains(text(), 'Login')]",       # 英文登录
                        "//input[@value='登入']",                   # 登入输入框
                        "//input[@value='登錄']",                   # 繁体登录输入框
                        "//input[@value='登录']",                   # 简体登录输入框
                        ".login-btn",
                        "#loginBtn"
                    ]
                    
                    login_button_found = False
                    for selector in login_button_selectors:
                        try:
                            if selector.startswith("//"):
                                elements = self.driver.find_elements(By.XPATH, selector)
                            else:
                                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                            
                            for element in elements:
                                if element.is_displayed() and element.is_enabled():
                                    button_text = element.text.strip()
                                    print(f"✅ 找到登入按钮: '{button_text}'")
                                    
                                    # 高亮并点击登入按钮
                                    self.driver.execute_script("""
                                        arguments[0].style.border = '3px solid #00ff00';
                                        arguments[0].style.backgroundColor = 'rgba(0,255,0,0.2)';
                                        arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});
                                    """, element)
                                    
                                    time.sleep(2)
                                    print(f"🖱️ 点击登入按钮: '{button_text}'")
                                    element.click()
                                    time.sleep(5)
                                    
                                    # 检查页面变化
                                    new_url = self.driver.current_url
                                    print(f"🌐 点击后URL: {new_url}")
                                    
                                    print("✅ 成功点击登入按钮！")
                                    print("🎉 应该已进入购票页面")
                                    
                                    login_button_found = True
                                    return True
                                    
                        except Exception as e:
                            continue
                    
                    if not login_button_found:
                        remaining = max_wait - i - check_interval
                        if remaining > 0:
                            print(f"🔍 未找到登入按钮，继续等待... (剩余 {remaining} 秒)")
                        else:
                            break
                    
                except Exception as e:
                    print(f"⚠️ 页面检查异常: {e}")
                    continue
            
            print("⚠️ 等待登入按钮超时")
            print("💡 请手动点击登入按钮进入购票页面")
            return False
            
        except Exception as e:
            print(f"❌ 等待Cloudflare和登入按钮异常: {e}")
            return False

    def _handle_additional_continue_button(self) -> bool:
        """处理登入后出现的额外继续按钮"""
        try:
            print("🔍 寻找登入后的继续按钮...")
            time.sleep(1)  # 等待页面加载新的按钮
            
            # 使用快速查找方法
            continue_button = self._fast_find_button('continue_btns', ['繼續', '继续', 'Continue'])
            
            if continue_button:
                text = continue_button.text.strip()
                print(f"✅ 找到继续按钮: '{text}'")
                
                # 高亮并使用JavaScript点击（避免被遮挡）
                self.driver.execute_script("""
                    arguments[0].style.border = '3px solid #00ff00';
                    arguments[0].style.backgroundColor = 'rgba(0,255,0,0.2)';
                    arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});
                """, continue_button)
                
                time.sleep(0.5)
                
                # 使用JavaScript点击，避免被footer等元素遮挡
                try:
                    self.driver.execute_script("arguments[0].click();", continue_button)
                    print(f"✅ 已点击继续按钮: '{text}' (JavaScript点击)")
                except Exception as e:
                    # 备用：尝试ActionChains点击
                    try:
                        from selenium.webdriver.common.action_chains import ActionChains
                        actions = ActionChains(self.driver)
                        actions.move_to_element(continue_button).click().perform()
                        print(f"✅ 已点击继续按钮: '{text}' (ActionChains点击)")
                    except Exception as e2:
                        print(f"❌ 继续按钮点击失败: JavaScript: {e}, ActionChains: {e2}")
                        return False
                
                # 等待页面跳转
                time.sleep(1.5)
                return True
            
            # 备用：传统方法
            continue_selectors = [
                "//button[contains(text(), '繼續') or contains(text(), '继续') or contains(text(), 'Continue')]",
                "//a[contains(text(), '繼續') or contains(text(), '继续')]"
            ]
            
            for selector in continue_selectors:
                try:
                    if selector.startswith("//"):
                        elements = self.driver.find_elements(By.XPATH, selector)
                    else:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            text = element.text.strip()
                            print(f"✅ 找到继续按钮: '{text}'")
                            
                            # 高亮并点击
                            self.driver.execute_script("""
                                arguments[0].style.border = '3px solid #00ff00';
                                arguments[0].style.backgroundColor = 'rgba(0,255,0,0.2)';
                                arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});
                            """, element)
                            
                            time.sleep(0.5)
                            element.click()
                            print(f"✅ 已点击继续按钮: '{text}'")
                            
                            # 等待页面跳转
                            time.sleep(1.5)
                            return True
                            
                except Exception as e:
                    continue
            
            print("⚠️ 未找到继续按钮")
            return False
            
        except Exception as e:
            print(f"❌ 处理继续按钮异常: {e}")
            return False

    def _check_for_additional_buttons(self) -> bool:
        """检查页面中是否出现了新的需要处理的按钮"""
        try:
            time.sleep(2)  # 等待页面动态加载
            
            # 检查是否出现了新的按钮（优先繁体中文）
            additional_button_selectors = [
                "//button[contains(text(), '登入')]",    # 通用登入
                "//button[contains(text(), '登錄')]",    # 繁体登录
                "//button[contains(text(), '登录')]",    # 简体登录
                "//a[contains(text(), '登入')]",        # 通用登入链接
                "//a[contains(text(), '登錄')]",        # 繁体登录链接
                "//a[contains(text(), '登录')]",        # 简体登录链接
                "//button[contains(text(), '確認')]",    # 繁体确认
                "//button[contains(text(), '确认')]",    # 简体确认
                "//button[contains(text(), '下一步')]",  # 下一步
                "//button[contains(text(), 'Login')]",   # 英文登录
                "//button[contains(text(), 'Next')]",    # 英文下一步
                "//button[contains(text(), 'Confirm')]", # 英文确认
                ".login-btn",
                ".confirm-btn"
            ]
            
            for selector in additional_button_selectors:
                try:
                    if selector.startswith("//"):
                        elements = self.driver.find_elements(By.XPATH, selector)
                    else:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            button_text = element.text.strip()
                            print(f"🔍 发现额外按钮: '{button_text}'")
                            
                            # 自动点击登入/确认类按钮（支持繁体中文）
                            if any(keyword in button_text for keyword in ['登入', '登錄', '登录', 'Login', '確認', '确认', '下一步', 'Next', 'Confirm']):
                                print(f"🎯 自动点击: '{button_text}'")
                                
                                # 高亮按钮
                                self.driver.execute_script("""
                                    arguments[0].style.border = '3px solid #00ff00';
                                    arguments[0].style.backgroundColor = 'rgba(0,255,0,0.2)';
                                """, element)
                                
                                time.sleep(1)
                                element.click()
                                time.sleep(3)
                                print(f"✅ 成功点击'{button_text}'按钮")
                                return True
                                
                except Exception as e:
                    continue
            
            return False
            
        except Exception as e:
            print(f"⚠️ 检查额外按钮异常: {e}")
            return False
    
    def _find_and_click_any_purchase_button(self) -> bool:
        """在活动详情页面持续寻找任何可能的购票相关按钮"""
        try:
            print("🔍 持续搜索购票相关按钮...")
            print("💡 将不断重复搜索直到找到按钮")
            
            max_search_time = 20  # 最多搜索20秒
            check_interval = 1    # 每1秒检查一次
            total_rounds = int(max_search_time / check_interval)
            
            # 更全面的按钮搜索策略
            purchase_button_strategies = [
                # 文本匹配（优先繁体中文）
                "//button[contains(text(), '購票') or contains(text(), '购票')]",
                "//a[contains(text(), '購票') or contains(text(), '购票')]",
                "//button[contains(text(), '購買') or contains(text(), '购买')]", 
                "//a[contains(text(), '購買') or contains(text(), '购买')]",
                "//button[contains(text(), '立即') and (contains(text(), '購') or contains(text(), '买'))]",
                "//button[contains(text(), '馬上') and (contains(text(), '購') or contains(text(), '买'))]",
                "//button[contains(text(), 'Buy') or contains(text(), 'Purchase')]",
                "//button[contains(text(), '登入')]",  # 添加登入按钮
                "//a[contains(text(), '登入')]",      # 添加登入链接
                "//button[contains(text(), '繼續')]", # 添加繼續按钮
                "//a[contains(text(), '繼續')]",      # 添加繼續链接
                
                # ID和class选择器
                "#buyTicketBtn", "#purchaseBtn", "#buyBtn", "#loginBtn",
                ".buy-button", ".purchase-button", ".ticket-button", ".btn-login",
                "button[class*='buy']", "button[class*='purchase']", "button[class*='ticket']", "button[class*='login']",
                
                # onclick事件
                "button[onclick*='buy']", "button[onclick*='purchase']", "button[onclick*='ticket']", "button[onclick*='login']",
                "a[onclick*='buy']", "a[onclick*='purchase']", "a[onclick*='ticket']", "a[onclick*='login']",
                
                # 通用按钮（最后尝试）
                "button", "a[role='button']"
            ]
            
            # 持续搜索循环
            for search_round in range(total_rounds):
                elapsed_time = search_round * check_interval
                
                # 每5秒显示一次进度
                if search_round % 5 == 0 and search_round > 0:
                    print(f"🔄 继续搜索购票按钮... 已搜索{elapsed_time}秒")
                
                    for strategy in purchase_button_strategies:
                        try:
                            if strategy.startswith("//"):
                                elements = self.driver.find_elements(By.XPATH, strategy)
                            else:
                                elements = self.driver.find_elements(By.CSS_SELECTOR, strategy)
                            
                            for element in elements:
                                if element.is_displayed() and element.is_enabled():
                                    text = element.text.strip()
                                    onclick = element.get_attribute('onclick') or ''
                                    
                                    # 检查是否是购票相关按钮
                                    purchase_keywords = ['購票', '购票', '購買', '购买', '立即', '馬上', 'buy', 'purchase', 'ticket', '登入', '繼續', '继续']
                                    exclude_keywords = ['facebook', 'google', 'wechat', '微信', 'share', '分享']
                                    
                                    if text:
                                        # 文本匹配
                                        has_purchase_keyword = any(keyword in text for keyword in purchase_keywords)
                                        has_exclude_keyword = any(keyword in text.lower() for keyword in exclude_keywords)
                                        
                                        if has_purchase_keyword and not has_exclude_keyword:
                                            print(f"✅ 找到目标按钮: '{text}' (第{search_round+1}轮搜索)")
                                            
                                            # 尝试点击
                                            try:
                                                self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
                                                time.sleep(0.3)
                                                self.driver.execute_script("arguments[0].click();", element)
                                                print(f"✅ 已点击按钮: '{text}'")
                                                
                                                # 等待页面反应
                                                time.sleep(2)
                                                new_url = self.driver.current_url
                                                print(f"🌐 点击后URL: {new_url}")
                                                
                                                # 检查是否成功跳转
                                                if "performance" in new_url:
                                                    print("🎯 检测到进入购票页面")
                                                    purchase_result = self.complete_purchase_flow()
                                                    if purchase_result.success:
                                                        print(f"🎉 {purchase_result.message}")
                                                    return True
                                                else:
                                                    print("🔄 点击后继续监控...")
                                                    break  # 跳出当前策略，进入下一轮搜索
                                                
                                            except Exception as e:
                                                print(f"❌ 点击按钮失败: {e}")
                                                continue
                                    
                                    # onclick事件匹配
                                    elif onclick and any(keyword in onclick.lower() for keyword in ['buy', 'purchase', 'ticket', 'go', 'login']):
                                        print(f"✅ 找到onclick按钮: onclick='{onclick[:50]}...' (第{search_round+1}轮搜索)")
                                        try:
                                            self.driver.execute_script("arguments[0].click();", element)
                                            print("✅ 已点击onclick按钮")
                                            time.sleep(2)
                                            
                                            # 检查跳转
                                            new_url = self.driver.current_url
                                            if "performance" in new_url:
                                                purchase_result = self.complete_purchase_flow()
                                                if purchase_result.success:
                                                    print(f"🎉 {purchase_result.message}")
                                                return True
                                            break  # 进入下一轮搜索
                                        except:
                                            continue
                                            
                        except Exception as e:
                            continue
                
                # 每轮搜索后检查页面状态
                current_url = self.driver.current_url
                if "performance" in current_url:
                    print(f"🎯 检测到页面已跳转到购票页面 (第{search_round+1}轮搜索)")
                    purchase_result = self.complete_purchase_flow()
                    if purchase_result.success:
                        print(f"🎉 {purchase_result.message}")
                    return True
                
                # 等待后进入下一轮搜索
                time.sleep(check_interval)
            
            print("⚠️ 未找到任何可点击的购票按钮")
            
            # 显示页面上所有可用按钮进行调试
            print("🔍 调试：显示页面上所有按钮...")
            self._show_all_buttons_debug()
            
            return False
            
        except Exception as e:
            print(f"❌ 扩展按钮搜索异常: {e}")
            return False
    
    def _show_all_buttons_debug(self):
        """显示页面上所有按钮的调试信息"""
        try:
            print("📊 调试信息：页面上所有按钮和链接")
            print("-" * 50)
            
            # 获取所有可能的按钮元素
            all_elements = []
            
            # 按钮元素
            buttons = self.driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                if btn.is_displayed():
                    text = btn.text.strip()
                    onclick = btn.get_attribute('onclick') or ''
                    class_name = btn.get_attribute('class') or ''
                    id_attr = btn.get_attribute('id') or ''
                    all_elements.append({
                        'type': 'button',
                        'text': text,
                        'onclick': onclick[:100] if onclick else '',
                        'class': class_name[:50] if class_name else '',
                        'id': id_attr
                    })
            
            # 链接元素
            links = self.driver.find_elements(By.TAG_NAME, "a")
            for link in links:
                if link.is_displayed():
                    text = link.text.strip()
                    href = link.get_attribute('href') or ''
                    onclick = link.get_attribute('onclick') or ''
                    class_name = link.get_attribute('class') or ''
                    id_attr = link.get_attribute('id') or ''
                    if text or onclick or 'button' in class_name.lower():
                        all_elements.append({
                            'type': 'link',
                            'text': text,
                            'href': href[:100] if href else '',
                            'onclick': onclick[:100] if onclick else '',
                            'class': class_name[:50] if class_name else '',
                            'id': id_attr
                        })
            
            print(f"发现 {len(all_elements)} 个可交互元素:")
            for i, elem in enumerate(all_elements[:15], 1):  # 只显示前15个
                print(f"\n{i}. 类型: {elem['type']}")
                if elem['text']:
                    print(f"   文本: '{elem['text'][:80]}'")
                if elem['id']:
                    print(f"   ID: '{elem['id']}'")
                if elem['class']:
                    print(f"   Class: '{elem['class']}'")
                if elem.get('href'):
                    print(f"   Href: '{elem['href']}'")
                if elem['onclick']:
                    print(f"   Onclick: '{elem['onclick']}'")
            
            print("\n" + "-" * 50)
            
        except Exception as e:
            print(f"❌ 调试信息获取失败: {e}")
    
    def _smart_wait_for_login_button(self, max_wait_time: int = 30) -> bool:
        """持续重复寻找登入按钮直到找到为止（解决页面加载慢的问题）"""
        try:
            print("⏳ 持续寻找登入按钮...")
            print("💡 将不断重复搜索直到找到按钮或达到最大等待时间")
            
            login_selectors = [
                ".btn-login",
                "button[onclick*='submitLogin']", 
                "button[onclick*='login']",
                "//button[contains(text(), '登入')]",
                "//button[contains(text(), '登錄')]", 
                "//button[contains(text(), '登录')]",
                "//a[contains(text(), '登入')]",
                "//a[contains(text(), '登錄')]",
                "//a[contains(text(), '登录')]",
                "//button[contains(text(), 'Login')]",
                "//button[contains(text(), '繼續')]",  # 添加繼續按钮
                "//button[contains(text(), '继续')]",   # 添加继续按钮
                "//a[contains(text(), '繼續')]",       # 添加繼續链接
                "//a[contains(text(), '继续')]"        # 添加继续链接
            ]
            
            check_interval = 1  # 每1秒检查一次
            total_checks = int(max_wait_time / check_interval)
            
            for check_round in range(total_checks):
                elapsed_time = check_round * check_interval
                
                # 每5秒显示一次进度
                if check_round % 5 == 0 and check_round > 0:
                    print(f"🔄 继续搜索... 已等待{elapsed_time}秒，还会继续尝试{max_wait_time - elapsed_time}秒")
                
                # 循环尝试所有选择器
                for selector_round, selector in enumerate(login_selectors):
                    try:
                        if selector.startswith("//"):
                            elements = self.driver.find_elements(By.XPATH, selector)
                        else:
                            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        
                        for element in elements:
                            if element.is_displayed() and element.is_enabled():
                                text = element.text.strip()
                                print(f"✅ 找到目标按钮: '{text}' (第{check_round+1}轮搜索，选择器{selector_round+1})")
                                
                                # 立即点击
                                try:
                                    self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
                                    time.sleep(0.3)
                                    self.driver.execute_script("arguments[0].click();", element)
                                    print("✅ 按钮点击完成 (持续搜索+JavaScript点击)")
                                    
                                    # 等待页面反应
                                    time.sleep(2)
                                    current_url = self.driver.current_url
                                    print(f"🌐 点击后URL: {current_url}")
                                    
                                    # 检查是否进入购票页面
                                    if "performance" in current_url:
                                        print("🎯 检测到购票页面，开始自动购票...")
                                        purchase_result = self.complete_purchase_flow()
                                        if purchase_result.success:
                                            print(f"🎉 {purchase_result.message}")
                                        else:
                                            print(f"⚠️ {purchase_result.message}")
                                        return True
                                    else:
                                        print("🔄 点击后继续监控页面变化...")
                                        # 继续下一轮搜索，页面可能还在加载
                                        break
                                    
                                except Exception as e:
                                    print(f"❌ 按钮点击失败: {e}")
                                    continue
                                    
                    except Exception as e:
                        continue
                
                # 每轮搜索后检查是否已经自动跳转
                current_url = self.driver.current_url
                if "performance" in current_url:
                    print(f"🎯 检测到页面已自动跳转到购票页面 (第{check_round+1}轮搜索)")
                    purchase_result = self.complete_purchase_flow()
                    if purchase_result.success:
                        print(f"🎉 {purchase_result.message}")
                    return True
                
                # 等待后继续下一轮搜索
                time.sleep(check_interval)
            
            print(f"⏰ 持续搜索{max_wait_time}秒后仍未找到目标按钮")
            print("💡 显示调试信息...")
            self._show_all_buttons_debug()
            return False
            
        except Exception as e:
            print(f"❌ 持续搜索按钮异常: {e}")
            return False
    
    def _analyze_page_structure(self):
        """分析购票页面结构"""
        try:
            current_url = self.driver.current_url
            page_title = self.driver.title
            print(f"📄 页面URL: {current_url}")
            print(f"📄 页面标题: {page_title}")
            
            # 分析表单结构
            forms = self.driver.find_elements(By.TAG_NAME, "form")
            print(f"📋 发现 {len(forms)} 个表单")
            
            # 分析所有select元素
            selects = self.driver.find_elements(By.TAG_NAME, "select")
            print(f"🔽 发现 {len(selects)} 个下拉选择框:")
            for i, select in enumerate(selects):
                try:
                    name = select.get_attribute('name') or '无名称'
                    id_attr = select.get_attribute('id') or '无ID'
                    options = select.find_elements(By.TAG_NAME, "option")
                    print(f"   选择框{i+1}: name='{name}', id='{id_attr}', {len(options)}个选项")
                    for j, option in enumerate(options[:5]):  # 只显示前5个选项
                        text = option.text.strip()
                        value = option.get_attribute('value')
                        print(f"     选项{j+1}: '{text}' (value='{value}')")
                except:
                    continue
            
            # 分析日期选择按钮
            date_elements = self.driver.find_elements(By.CSS_SELECTOR, ".date-box, [class*='date'], button[class*='date']")
            print(f"📅 发现 {len(date_elements)} 个日期相关元素:")
            for i, elem in enumerate(date_elements):
                try:
                    text = elem.text.strip()
                    class_attr = elem.get_attribute('class') or '无class'
                    print(f"   日期元素{i+1}: '{text}' (class='{class_attr}')")
                except:
                    continue
            
            # 分析票价按钮（寻找ticketPrice相关元素）
            print("🎟️ 分析票价按钮:")
            price_patterns = [
                "[id*='ticketPrice']",
                "[id*='price']", 
                "button[class*='price']",
                ".price-button",
                "[onclick*='price']"
            ]
            
            all_price_elements = []
            for pattern in price_patterns:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, pattern)
                    all_price_elements.extend(elements)
                except:
                    continue
            
            print(f"   发现 {len(all_price_elements)} 个票价相关元素:")
            for i, elem in enumerate(all_price_elements[:10]):  # 只显示前10个
                try:
                    id_attr = elem.get_attribute('id') or '无ID'
                    class_attr = elem.get_attribute('class') or '无class'
                    text = elem.text.strip()[:50]  # 限制文本长度
                    onclick = elem.get_attribute('onclick') or ''
                    onclick = onclick[:30] + '...' if len(onclick) > 30 else onclick
                    print(f"     票价{i+1}: id='{id_attr}', text='{text}', onclick='{onclick}'")
                except:
                    continue
            
            # 分析购买按钮
            buy_patterns = [
                "#expressPurchaseBtn",
                "button[class*='purchase']",
                "button[class*='buy']",
                "button[class*='express']",
                "[onclick*='purchase']"
            ]
            
            print("🛒 分析购买按钮:")
            for pattern in buy_patterns:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, pattern)
                    for i, elem in enumerate(elements):
                        id_attr = elem.get_attribute('id') or '无ID'
                        text = elem.text.strip()
                        print(f"   购买按钮: id='{id_attr}', text='{text}', selector='{pattern}'")
                except:
                    continue
            
            # 分析数量选择（包括ticketType类型）
            qty_elements = self.driver.find_elements(By.CSS_SELECTOR, "select[name*='qty'], input[name*='qty'], [name*='quantity'], select[name*='ticketType']")
            print(f"🔢 发现 {len(qty_elements)} 个数量选择元素:")
            for i, elem in enumerate(qty_elements):
                try:
                    name = elem.get_attribute('name') or '无名称'
                    tag = elem.tag_name
                    print(f"   数量元素{i+1}: {tag}, name='{name}'")
                except:
                    continue
                    
        except Exception as e:
            print(f"❌ 页面结构分析异常: {e}")
    
    def _handle_venue_page(self) -> bool:
        """处理venue页面（保持兼容性）"""
        return self._handle_venue_continue_flow()
    
    def _auto_select_ticket(self) -> bool:
        """自动选择票型和数量（参考项目实现）"""
        try:
            print("🎫 开始自动选票流程...")
            time.sleep(1)  # 减少等待时间，加快速度
            
            # 先获取配置信息
            ticket_prefs = self.config.get('ticket_preferences', {})
            preferred_zones = ticket_prefs.get('preferred_zones', ['VIP', 'A区', 'B区'])
            quantity = ticket_prefs.get('quantity', 2)
            
            # 跟踪数量选择是否已处理
            quantity_handled = False
            
            # 调试：分析页面结构（可选，注释掉以加快速度）
            # print("🔍 分析购票页面结构...")
            # self._analyze_page_structure()
            
            # 1. 分析ticketType0是票型还是数量
            try:
                dropdown_element = self.driver.find_element(By.NAME, "ticketType0")
                if dropdown_element:
                    select = Select(dropdown_element)
                    options = select.options
                    option_texts = [opt.text.strip() for opt in options]
                    option_values = [opt.get_attribute('value') for opt in options]
                    
                    print(f"📋 发现ticketType0下拉框")
                    print(f"   选项文本: {option_texts}")
                    print(f"   选项值: {option_values}")
                    
                    # 判断这是数量选择还是票型选择
                    # 如果所有选项都是数字且范围较大（如0-40），很可能是数量选择
                    all_numeric = all(val.isdigit() for val in option_values if val)
                    max_value = max(int(val) for val in option_values if val.isdigit()) if any(val.isdigit() for val in option_values) else 0
                    
                    if all_numeric and max_value >= 2:  # 如果是0-6这样的数字范围，肯定是数量选择
                        print("   判定: 这是数量选择框")
                        
                        # 直接使用数量作为索引（简化逻辑）
                        target_quantity = quantity
                        
                        # 确保数量在有效范围内
                        if target_quantity >= len(options):
                            target_quantity = len(options) - 1
                        elif target_quantity < 0:
                            target_quantity = 1
                        
                        try:
                            # 直接通过索引选择（数字就是索引）
                            select.select_by_index(target_quantity)
                            print(f"✅ 已设置购票数量: {target_quantity}")
                            quantity_handled = True
                        except Exception as e:
                            print(f"❌ 数量设置失败: {e}")
                            # 备用方案：选择索引1（1张票）
                            try:
                                select.select_by_index(1)
                                print(f"✅ 已设置购票数量: 1 (备用)")
                                quantity_handled = True
                            except:
                                print("❌ 备用数量设置也失败")
                    else:
                        print("   判定: 这是票型选择框")
                        # 选择第二个选项（跳过第一个可能是"请选择"）
                        if len(options) > 1:
                            select.select_by_index(1)
                            print(f"✅ 已选择票型: {option_texts[1]}")
                    
                    time.sleep(0.3)  # 减少等待时间
            except Exception as e:
                print(f"ℹ️ ticketType0处理异常: {e}")
            
            # 2. 选择日期（如果有多个场次）
            try:
                if self._select_preferred_session():
                    print("✅ 智能场次策略已执行")
                else:
                    print("⚠️ 智能场次策略未能完成，继续使用备用日期选择")

                # 更精确的日期选择检测
                date_boxes = self.driver.find_elements(By.CLASS_NAME, "date-box")
                clickable_dates = []
                
                # 只检测真正可点击且包含日期内容的元素
                for date_box in date_boxes:
                    if date_box.is_displayed() and date_box.is_enabled():
                        text = date_box.text.strip()
                        # 检查是否包含日期相关内容（数字、月份等）
                        if text and (any(char.isdigit() for char in text) or 
                                   any(month in text for month in ['一月', '二月', '三月', '四月', '五月', '六月', 
                                                                 '七月', '八月', '九月', '十月', '十一月', '十二月',
                                                                 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                                                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])):
                            clickable_dates.append((date_box, text))
                
                if len(clickable_dates) > 1:
                    print(f"📅 发现 {len(clickable_dates)} 个可选场次日期")
                    # 选择第一个可用日期
                    date_box, text = clickable_dates[0]
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", date_box)
                    time.sleep(0.3)
                    date_box.click()
                    print(f"✅ 已选择日期: {text[:30]}...")
                elif len(clickable_dates) == 1:
                    print("ℹ️ 只有一个场次，无需选择日期")
                else:
                    print("ℹ️ 未发现需要选择的日期")
                    
                time.sleep(0.3)
            except:
                print("ℹ️ 日期选择检测异常，跳过")
            
            # 3. 选择票价区域
            print(f"🎯 寻找票价选项...")
            print(f"   偏好区域: {preferred_zones}")
            print(f"   购票数量: {quantity}")
            
            # 改进的票价选择逻辑 - 从最低价开始递增选择
            ticket_selected = False
            
            # 扫描所有票价元素并排序
            all_price_elements = []
            for i in range(10):  # 检查ticketPrice0到ticketPrice9
                try:
                    price_element = self.driver.find_element(By.ID, f"ticketPrice{i}")
                    element_text = ""
                    try:
                        parent = price_element.find_element(By.XPATH, "..")
                        element_text = parent.text.strip()
                        if not element_text:
                            label_element = self.driver.find_element(By.CSS_SELECTOR, f"label[for='ticketPrice{i}']")
                            element_text = label_element.text.strip()
                    except:
                        try:
                            table_row = price_element.find_element(By.XPATH, "./ancestor::tr")
                            element_text = table_row.text.strip()
                        except:
                            element_text = f"票价选项{i}"
                    
                    price_num = 0
                    import re
                    price_matches = re.findall(r'[\$\￥￥]?(\d+)', element_text)
                    if price_matches:
                        price_num = int(price_matches[-1])
                    
                    is_sold_out = any(kw in element_text for kw in ['售罄', 'Sold Out', 'sold out', 'Unavailable', '无票'])
                    
                    all_price_elements.append({
                        'element': price_element,
                        'text': element_text,
                        'index': i,
                        'price': price_num,
                        'sold_out': is_sold_out,
                    })
                    
                except:
                    continue
            
            if not all_price_elements:
                print("   ❌ 未发现票价元素，尝试强制选择...")
            else:
                print(f"   📊 发现 {len(all_price_elements)} 个票价选项:")
                for pe in all_price_elements:
                    sold_tag = " [售罄]" if pe['sold_out'] else ""
                    print(f"     票价{pe['index']}: '{pe['text']}' (¥{pe['price']}{sold_tag})")

                sorted_by_price = sorted([pe for pe in all_price_elements if not pe['sold_out']], key=lambda x: x['price'])
                sold_out_items = [pe for pe in all_price_elements if pe['sold_out']]

                selected_price = None
                for price_info in sorted_by_price:
                    try:
                        element = price_info['element']
                        if element.is_displayed() and element.is_enabled():
                            self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});", element)
                            self.driver.execute_script("arguments[0].click();", element)
                            print(f"✅ 已选择票价: '{price_info['text']}' (¥{price_info['price']})")
                            ticket_selected = True
                            selected_price = price_info
                            break
                        else:
                            print(f"   ⚠️ 票价{price_info['index']}不可用(被禁用/不可见)，跳过")
                    except Exception as e:
                        print(f"   ⚠️ 点击票价{price_info['index']}失败: {e}")
                        continue

                if not ticket_selected and sold_out_items:
                    print(f"   ⚠️ 所有可用票价均不可选，尝试选择第一个可点击选项...")
                    for price_info in sorted_by_price:
                        try:
                            self.driver.execute_script("arguments[0].click();", price_info['element'])
                            print(f"✅ 已选择票价: '{price_info['text']}' (强制点击)")
                            ticket_selected = True
                            break
                        except Exception:
                            continue

            if not ticket_selected:
                print("❌ 无法选择票价")
                return False
            
            # 4. 设置购票数量（仅在未处理时执行）
            if not quantity_handled:
                print("🔢 寻找其他数量选择...")
            else:
                print("✅ 数量选择已完成，跳过额外搜索")
            
            if not quantity_handled:
                try:
                    # 扩展数量选择器，包括所有可能的select元素
                    qty_selectors = [
                        "select[name*='qty']",
                        "select[name*='quantity']", 
                        "select[name*='num']",
                        "select[name*='count']",
                        "select[id*='qty']",
                        "select[id*='quantity']"
                    ]
                    
                    quantity_found = False
                    for selector in qty_selectors:
                        try:
                            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                            for elem in elements:
                                if elem.is_displayed():
                                    select_obj = Select(elem)
                                    options = select_obj.options
                                    option_texts = [opt.text.strip() for opt in options if opt.text.strip()]
                                    print(f"     发现数量选择框，选项: {option_texts}")
                                    
                                    # 尝试选择指定数量（改进版）
                                    target_quantity = min(quantity, len(options) - 1)
                                    
                                    # 方法1：通过文本或value匹配
                                    for opt in options:
                                        opt_text = opt.text.strip()
                                        opt_value = opt.get_attribute('value')
                                        if opt_text == str(target_quantity) or opt_value == str(target_quantity):
                                            select_obj.select_by_value(opt_value)
                                            print(f"✅ 已设置购票数量: {target_quantity} (匹配: text='{opt_text}', value='{opt_value}')")
                                            quantity_found = True
                                            break
                                    
                                    # 方法2：通过索引直接选择
                                    if not quantity_found and target_quantity < len(options):
                                        try:
                                            select_obj.select_by_index(target_quantity)
                                            selected_option = options[target_quantity]
                                            print(f"✅ 已设置购票数量: {target_quantity} (索引选择: {selected_option.text})")
                                            quantity_found = True
                                        except Exception as e:
                                            print(f"     索引选择失败: {e}")
                                    
                                    # 方法3：备用选择
                                    if not quantity_found and len(options) > 1:
                                        select_obj.select_by_index(1)
                                        print(f"✅ 已设置购票数量: {options[1].text} (备用)")
                                        quantity_found = True
                                    
                                    if quantity_found:
                                        break
                        
                            if quantity_found:
                                break
                        except Exception as e:
                            print(f"     选择器 {selector} 异常: {e}")
                            continue
                
                    # 如果上面都没找到，检查所有select元素
                    if not quantity_found:
                        print("     检查所有select元素...")
                        all_selects = self.driver.find_elements(By.TAG_NAME, "select")
                        for i, elem in enumerate(all_selects):
                            try:
                                if elem.is_displayed():
                                    # 跳过已经处理过的ticketType0
                                    elem_name = elem.get_attribute('name') or ''
                                    if elem_name == 'ticketType0':
                                        print(f"     跳过已处理的ticketType0")
                                        continue
                                    
                                    select_obj = Select(elem)
                                    options = select_obj.options
                                    option_texts = [opt.text.strip() for opt in options if opt.text.strip()]
                                    
                                    # 如果选项看起来像数量选择（包含数字）
                                    if any(text.isdigit() and int(text) <= 10 for text in option_texts):
                                        print(f"     可能的数量选择框{i}: {option_texts}")
                                        
                                        # 尝试选择合适的数量
                                        target_quantity = min(quantity, len(options) - 1)  # 确保不超出范围
                                        
                                        # 方法1：通过value选择
                                        for opt in options:
                                            opt_text = opt.text.strip()
                                            opt_value = opt.get_attribute('value')
                                            if opt_text == str(target_quantity) or opt_value == str(target_quantity):
                                                select_obj.select_by_value(opt_value)
                                                print(f"✅ 已设置购票数量: {target_quantity} (通过value: {opt_value})")
                                                quantity_found = True
                                                break
                                        
                                        # 方法2：如果value方法失败，通过索引选择
                                        if not quantity_found and target_quantity < len(options):
                                            try:
                                                select_obj.select_by_index(target_quantity)
                                                selected_option = options[target_quantity]
                                                print(f"✅ 已设置购票数量: {target_quantity} (通过索引，选项: {selected_option.text})")
                                                quantity_found = True
                                            except Exception as idx_e:
                                                print(f"     通过索引选择失败: {idx_e}")
                                        
                                        # 方法3：如果还是失败，选择第二个选项（跳过第一个"0"）
                                        if not quantity_found and len(options) > 1:
                                            try:
                                                select_obj.select_by_index(1)
                                                print(f"✅ 已设置购票数量: {options[1].text} (备用选择)")
                                                quantity_found = True
                                            except:
                                                pass
                                        
                                        if quantity_found:
                                            break
                            except Exception as e:
                                continue
                
                    if not quantity_found:
                        print("ℹ️ 未发现额外的数量选择元素")
                    else:
                        quantity_handled = True
                        
                except Exception as e:
                    print(f"❌ 数量选择异常: {e}")
            
            time.sleep(0.5)
            
            print("✅ 票务选择完成")
            if quantity_handled:
                print(f"🎯 已成功设置购票数量: {quantity}")
            else:
                print("⚠️ 未能设置数量，可能使用页面默认值")
            
            # 可配置的订单提交；默认仅选票，避免误触不可逆购买动作。
            if self.config.get('purchase_settings', {}).get('auto_submit_order', False):
                if self._auto_submit_order():
                    print("🎉 订单已自动提交！")
                else:
                    print("⚠️ 自动提交失败，请手动完成")
                    self._capture_diagnostics("auto_submit_failed")
            else:
                print("🛑 已完成自动选票；auto_submit_order=false，保留给用户手动确认提交")
            
            return True
            
        except Exception as e:
            print(f"❌ 自动选票异常: {e}")
            return False
    
    def _auto_submit_order(self) -> bool:
        """自动提交订单"""
        try:
            print("🚀 开始自动提交订单...")
            
            # 查找确定按钮（支持繁体中文）
            submit_selectors = [
                "#expressPurchaseBtn",  # 主要的购买按钮ID
                "button[onclick*='expressPurchase']",
                "//button[contains(text(), '確定')]",
                "//button[contains(text(), '确定')]",
                "//button[contains(text(), '提交')]",
                "//button[contains(text(), '購買')]",
                "//button[contains(text(), '购买')]",
                "button.btn-purchase",
                "button.btn-submit",
                "button[type='submit']"
            ]
            
            submit_button = None
            
            # 尝试各种选择器
            for selector in submit_selectors:
                try:
                    if selector.startswith("//"):
                        elements = self.driver.find_elements(By.XPATH, selector)
                    else:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            text = element.text.strip()
                            # 检查按钮文本是否包含确定、提交等关键词
                            if any(keyword in text for keyword in ['確定', '确定', '提交', '購買', '购买', 'Submit', 'Confirm']):
                                submit_button = element
                                print(f"✅ 找到提交按钮: '{text}'")
                                break
                            # 对于ID选择器，即使没有文本也接受
                            elif selector == "#expressPurchaseBtn":
                                submit_button = element
                                print(f"✅ 找到提交按钮 (ID: expressPurchaseBtn)")
                                break
                    
                    if submit_button:
                        break
                        
                except Exception as e:
                    continue
            
            if not submit_button:
                print("❌ 未找到提交按钮")
                return False
            
            # 高亮并点击提交按钮
            try:
                self.driver.execute_script("""
                    arguments[0].style.border = '3px solid #ff0000';
                    arguments[0].style.backgroundColor = 'rgba(255,0,0,0.2)';
                    arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});
                """, submit_button)
                
                time.sleep(0.5)
                
                # 使用JavaScript点击，避免被遮挡
                self.driver.execute_script("arguments[0].click();", submit_button)
                print("✅ 已点击提交按钮！")
                
                # 等待页面响应
                time.sleep(2)
                
                # 检查是否成功提交
                current_url = self.driver.current_url
                if "payment" in current_url or "confirm" in current_url or "success" in current_url:
                    print("🎉 订单提交成功！已进入支付或确认页面")
                    return True
                else:
                    print("⚠️ 订单提交后页面状态待确认")
                    return True
                    
            except Exception as e:
                print(f"❌ 点击提交按钮失败: {e}")
                # 尝试常规点击
                try:
                    submit_button.click()
                    print("✅ 已使用常规方式点击提交按钮")
                    return True
                except:
                    return False
                    
        except Exception as e:
            print(f"❌ 自动提交订单异常: {e}")
            return False
    
    def _fill_purchase_form(self) -> bool:
        """填写购票表单（参考项目实现）"""
        try:
            print("📝 开始填写购票信息...")
            time.sleep(2)
            
            # 1. 勾选条款复选框
            try:
                checkboxes = self.driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
                for checkbox in checkboxes:
                    if not checkbox.is_selected() and checkbox.is_displayed():
                        self.driver.execute_script("arguments[0].click();", checkbox)
                        print("✅ 已勾选条款")
                        time.sleep(0.5)
            except:
                print("ℹ️ 未发现需要勾选的条款")
            
            # 2. 填写取票密码（如果需要）
            ticket_password = self.config.get('purchase_settings', {}).get('ticket_password', '123456')
            try:
                password_inputs = self.driver.find_elements(By.CSS_SELECTOR, "input[type='password'], input[name*='password']")
                for pwd_input in password_inputs:
                    if pwd_input.is_displayed():
                        pwd_input.clear()
                        pwd_input.send_keys(ticket_password)
                        print(f"✅ 已填写取票密码")
                        time.sleep(0.5)
            except:
                print("ℹ️ 未发现取票密码输入框")
            
            # 3. 选择支付方式
            payment_method = self.config.get('purchase_settings', {}).get('payment_method', 'visa')
            if payment_method == 'alipay':
                try:
                    alipay_button = self.driver.find_element(By.CSS_SELECTOR, "[data-payment-code='ALIPAY']")
                    alipay_button.click()
                    print("✅ 已选择支付宝付款")
                except:
                    print("⚠️ 未找到支付宝选项")
            else:
                # 默认使用信用卡
                try:
                    visa_button = self.driver.find_element(By.CSS_SELECTOR, "[data-payment-code='VISA']")
                    visa_button.click()
                    print("✅ 已选择信用卡付款")
                except:
                    print("ℹ️ 使用默认支付方式")
            
            time.sleep(1)
            return True
            
        except Exception as e:
            print(f"❌ 填写购票信息异常: {e}")
            return False
    
    def _submit_purchase(self) -> bool:
        """提交购票（参考项目实现）"""
        try:
            print("💳 准备提交购票...")
            
            # 查找并点击确认/提交按钮
            submit_selectors = [
                "#proceedDisplay button",
                "button[type='submit']",
                "//button[contains(text(), '确认')]",
                "//button[contains(text(), '提交')]",
                "//button[contains(text(), '去付款')]",
                "//button[contains(text(), 'Proceed')]",
                "//button[contains(text(), 'Confirm')]"
            ]
            
            for selector in submit_selectors:
                try:
                    if selector.startswith("//"):
                        submit_btn = self.driver.find_element(By.XPATH, selector)
                    else:
                        submit_btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                    
                    if submit_btn.is_displayed() and submit_btn.is_enabled():
                        self.driver.execute_script("arguments[0].scrollIntoView(true);", submit_btn)
                        time.sleep(1)
                        
                        # 最后确认
                        if self.config.get('purchase_settings', {}).get('auto_purchase', False):
                            print("🚀 自动提交购票...")
                            submit_btn.click()
                            print("✅ 已提交购票！")
                            
                            # 等待页面跳转
                            time.sleep(5)
                            
                            # 截图保存
                            timestamp = time.strftime("%Y%m%d-%H%M%S")
                            self.driver.save_screenshot(f"purchase_success_{timestamp}.png")
                            print(f"📸 已保存购票截图: purchase_success_{timestamp}.png")
                            
                            return True
                        else:
                            print("⚠️ 自动购票未启用，请手动点击提交按钮")
                            print("💡 如需启用自动购票，请在配置文件中设置 auto_purchase: true")
                            return False
                except:
                    continue
            
            print("❌ 未找到提交按钮")
            return False
            
        except Exception as e:
            print(f"❌ 提交购票异常: {e}")
            return False
    
    def complete_purchase_flow(self) -> PurchaseResult:
        """完成购票流程"""
        try:
            print("💳 开始完成购票流程...")
            
            # 检查当前页面
            current_url = self.driver.current_url
            print(f"📍 当前页面: {current_url}")
            
            # 判断是否在购票页面
            if "performance" in current_url and "venue.cityline.com" in current_url:
                print("✅ 已进入购票页面")

                if self._auto_select_ticket():
                    auto_submit = self.config.get('purchase_settings', {}).get('auto_submit_order', False)
                    next_step = "订单已按配置尝试提交" if auto_submit else "已停止在提交前，请手动确认后续购票和支付"
                    return PurchaseResult(
                        success=True,
                        message=f"票务选择完成！{next_step}"
                    )
                else:
                    self._capture_diagnostics("ticket_selection_failed")
                    return PurchaseResult(
                        success=False,
                        message="票务选择失败"
                    )
            else:
                print("⚠️ 未在购票页面，无法执行自动购票")
                self._capture_diagnostics("not_purchase_page")
            
            purchase_settings = self.config.get('purchase_settings', {})
            
            if not purchase_settings.get('auto_purchase', False):
                print("⚠️ 自动购票已禁用，需要手动完成")
                print("💡 请手动完成以下步骤：")
                print("   1. 选择座位")
                print("   2. 确认购票数量")
                print("   3. 填写购票信息")
                print("   4. 选择支付方式")
                print("   5. 完成支付")
                
                # 等待用户手动操作
                try:
                    input("按Enter键继续（完成手动购票后）...")
                except (EOFError, KeyboardInterrupt):
                    print("\n⚠️ 输入被中断，继续流程...")
                
                return PurchaseResult(
                    success=True,
                    message="手动购票模式，等待用户完成操作"
                )
            
            return PurchaseResult(
                success=False,
                message="当前页面不是可识别的购票页面，已保存诊断信息"
            )
            
        except Exception as e:
            return PurchaseResult(
                success=False,
                message=f"购票流程异常: {e}"
            )
    
    def run_complete_flow(self) -> bool:
        """运行完整的购票流程（参考项目风格的完整版）"""
        try:
            print("🎬 启动完整购票流程")
            self._mark_metric('flow_start')
            print("=" * 50)
            print("💡 类似参考项目的自动化流程")
            print("🚀 增强功能：智能检测 + 优先级排序 + 多重验证")
            print()
            
            # 显示配置概览
            self._display_config_summary()
            
            # 1. 创建浏览器
            print("🔧 第1步：创建浏览器实例")
            if not self.create_browser():
                return False
            self._mark_metric('browser_ready')
            print("✅ 浏览器创建成功")
            
            choice = ""  # 初始化choice变量
            
            try:
                # 第2步：访问活动页面并处理登录
                print("🌐 第2步：访问活动页面")
                if not self.access_event_page():
                    return False
                self._mark_metric('event_access_complete')
                
                # 检查是否已经在购票页面（可能在venue流程后直接跳转）
                current_url = self.driver.current_url
                if "performance" in current_url:
                    print("🎯 检测到购票页面，开始自动购票...")
                    purchase_result = self.complete_purchase_flow()
                    if purchase_result.success:
                        print(f"🎉 {purchase_result.message}")
                    else:
                        print(f"⚠️ {purchase_result.message}")
                else:
                    print("✅ 购票流程执行完成！")
                    print("💡 如果进入了购票页面，请手动完成选座和支付")
                
                # 保持浏览器开放
                if self.config.get('runtime', {}).get('non_interactive', False):
                    choice = 'close'
                    print("\n🤖 非交互模式：流程结束后自动关闭浏览器")
                else:
                    try:
                        choice = input("\n按Enter关闭浏览器，或输入'keep'保持开放: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print("\n⚠️ 输入被中断，默认保持浏览器开放")
                        choice = 'keep'
                if choice == 'keep':
                    print("🔄 浏览器保持开放")
                    return True
                
                return True
                
            finally:
                if self.driver and choice != 'keep':
                    if self.config.get('cloudflare', {}).get('keep_browser_open_on_failure', True) and self._detect_cloudflare():
                        print("🔄 检测到仍在Cloudflare验证页，保持浏览器开放方便手动处理")
                        self._save_metrics()
                        return True
                    self.driver.quit()
                    print("🔚 浏览器已关闭")
                self._save_metrics()
                    
        except Exception as e:
            print(f"❌ 完整流程异常: {e}")
            self._capture_diagnostics("complete_flow_failed")
            self._save_metrics()
            return False
    
    def _display_config_summary(self):
        """显示配置概览（简化版）"""
        try:
            target_event = self.config.get('target_event', {})
            ticket_prefs = self.config.get('ticket_preferences', {})
            purchase_settings = self.config.get('purchase_settings', {})
            
            # 从URL中提取活动名称
            url = target_event.get('url', '未配置')
            activity_name = "未配置"
            if url and url != "未配置":
                # 从URL中提取活动名称
                if "nctdreamthefuturehk" in url:
                    activity_name = "NCT DREAM THE FUTURE 香港演唱会"
                elif "sekainoowariphoenix" in url:
                    activity_name = "SEKAI NO OWARI Phoenix演唱会"
                else:
                    activity_name = "Cityline演出"
            
            print("📋 当前配置概览：")
            print(f"   🎯 目标活动: {activity_name}")
            print(f"   🌐 活动URL: {url}")
            print(f"   🎫 购票数量: {ticket_prefs.get('quantity', 1)}")
            print(f"   💰 偏好区域: {ticket_prefs.get('preferred_zones', [])}")
            print(f"   🤖 自动购买: {'是' if purchase_settings.get('auto_purchase', False) else '否（需手动确认）'}")
            print(f"   ⏱️ 最大等待: {purchase_settings.get('max_wait_time', 300)}秒")
            print()
            
        except Exception as e:
            print(f"⚠️ 配置显示异常: {e}")
    
    def _send_success_notification(self):
        """发送成功通知"""
        try:
            notifications = self.config.get('notifications', {})
            
            if notifications.get('success_sound', False):
                # 播放系统提示音
                print("\a")  # 系统提示音
            
            success_msg = notifications.get('success_message', '🎉 购票成功！')
            print(f"\n{success_msg}")
            
        except Exception as e:
            print(f"⚠️ 通知发送异常: {e}")


def main():
    """主函数"""
    print("🎫 增强版独立购票系统")
    print("=" * 50)
    print("💡 最接近参考项目的完整实现")
    print("🚀 功能特点:")
    print("   ✅ 智能'前　往　购　票'按钮检测")
    print("   ✅ 优先级排序的选择策略")
    print("   ✅ 完善的venue页面继续流程")
    print("   ✅ 多重验证和错误恢复")
    print("   ✅ 配置驱动的灵活系统")
    print()
    
    # 创建购票器实例
    purchaser = ConfigDrivenTicketPurchaser()
    
    # 运行完整流程
    success = purchaser.run_complete_flow()
    
    if success:
        print("\n🎉 增强版购票系统运行完成！")
        print("💡 这是最接近参考项目的完整实现")
        print("🔧 如有问题可继续完善配置文件")
    else:
        print("\n❌ 购票流程失败")
        print("💡 建议检查:")
        print("   - 网络连接状态")
        print("   - enhanced_config.json 配置")
        print("   - 活动URL有效性")
        print("   - Chrome浏览器版本")


if __name__ == "__main__":
    main()
