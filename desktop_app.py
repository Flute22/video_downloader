"""
MediaGrab Desktop — Universal Social Media Downloader
A standalone desktop app built with CustomTkinter.
"""

import os
import sys
import re
import threading
import subprocess
import platform
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
from PIL import Image
import requests
import yt_dlp
import instaloader

# ─── Resolve base paths for PyInstaller compatibility ────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Try to locate ffmpeg
FFMPEG_PATH = None
for candidate in [
    os.path.expanduser('~/AppData/Local/Microsoft/WinGet/Links'),
    os.path.join(BASE_DIR, 'ffmpeg'),
    '',  # rely on PATH
]:
    if candidate == '' or os.path.isdir(candidate):
        FFMPEG_PATH = candidate if candidate else None
        break


# ═══════════════════════════════════════════════════════════════════════════════
# Universal Downloader (extracted from app.py)
# ═══════════════════════════════════════════════════════════════════════════════

class UniversalDownloader:
    """Download media from multiple social platforms."""

    PLATFORM_LABELS = {
        'youtube':   '▶  YouTube',
        'instagram': '📷  Instagram',
        'facebook':  '📘  Facebook',
        'twitter':   '🐦  Twitter / X',
        'tiktok':    '🎵  TikTok',
        'pinterest': '📌  Pinterest',
        'linkedin':  '💼  LinkedIn',
        'snapchat':  '👻  Snapchat',
        'reddit':    '🤖  Reddit',
        'twitch':    '🎮  Twitch',
        'unknown':   '🌐  Other',
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/91.0.4472.124 Safari/537.36'
            )
        })

    # ── platform detection ────────────────────────────────────────────────
    def detect_platform(self, url: str) -> str:
        url_lower = url.lower()
        mapping = {
            'youtube':   ['youtube.com', 'youtu.be'],
            'instagram': ['instagram.com'],
            'facebook':  ['facebook.com', 'fb.watch'],
            'twitter':   ['twitter.com', 'x.com'],
            'tiktok':    ['tiktok.com'],
            'pinterest': ['pinterest.com'],
            'linkedin':  ['linkedin.com'],
            'snapchat':  ['snapchat.com'],
            'reddit':    ['reddit.com'],
            'twitch':    ['twitch.tv'],
        }
        for plat, domains in mapping.items():
            if any(d in url_lower for d in domains):
                return plat
        return 'unknown'

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _safe_filename(name: str, max_len: int = 100) -> str:
        name = re.sub(r'[<>:"/\\|?*]', '_', name).strip()
        return name[:max_len]

    @staticmethod
    def _extract_instagram_shortcode(url: str):
        for pat in [r'/p/([^/?]+)', r'/reel/([^/?]+)', r'/tv/([^/?]+)']:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _extract_instagram_username(url: str):
        m = re.search(r'instagram\.com/([^/?]+)', url)
        return m.group(1) if m else None

    # ── per-platform downloaders ──────────────────────────────────────────
    def _ydl_opts(self, path: str, template: str, fmt: str = 'best'):
        opts = {
            'outtmpl': os.path.join(path, template),
            'format': fmt,
            'merge_output_format': 'mp4',
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
        }
        if FFMPEG_PATH:
            opts['ffmpeg_location'] = FFMPEG_PATH
        return opts

    def download_youtube(self, url, path, progress_cb=None):
        opts = self._ydl_opts(
            path,
            '%(uploader)s - %(title)s.%(ext)s',
            'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
        )
        if progress_cb:
            opts['progress_hooks'] = [progress_cb]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and 'entries' in info:
                titles = [e.get('title', '?') for e in info['entries'] if e]
                return {'status': 'success', 'message': f'Downloaded {len(titles)} videos from playlist'}
            title = info.get('title', 'Unknown') if info else 'Unknown'
            return {'status': 'success', 'message': f'Downloaded: {title}'}

    def download_instagram(self, url, path, progress_cb=None):
        loader = instaloader.Instaloader(
            dirname_pattern=path,
            filename_pattern='{profile}_{mediaid}_{date_utc}',
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=True,
            compress_json=False,
        )
        if '/stories/' in url:
            username = self._extract_instagram_username(url)
            if username:
                profile = instaloader.Profile.from_username(loader.context, username)
                for story in loader.get_stories([profile.userid]):
                    for item in story.get_items():
                        loader.download_storyitem(item, target=username)
                return {'status': 'success', 'message': f'Stories downloaded for @{username}'}
        elif any(x in url for x in ['/reel/', '/p/', '/tv/']):
            shortcode = self._extract_instagram_shortcode(url)
            post = instaloader.Post.from_shortcode(loader.context, shortcode)
            loader.download_post(post, target=post.owner_username)
            kind = 'reel' if post.is_video else 'post'
            return {'status': 'success', 'message': f'Instagram {kind} downloaded from @{post.owner_username}'}
        else:
            username = self._extract_instagram_username(url)
            profile = instaloader.Profile.from_username(loader.context, username)
            count = 0
            for post in profile.get_posts():
                if count >= 10:
                    break
                loader.download_post(post, target=username)
                count += 1
            return {'status': 'success', 'message': f'Downloaded {count} posts from @{username}'}

    def _generic_download(self, url, path, label, template, progress_cb=None):
        opts = self._ydl_opts(path, template)
        if progress_cb:
            opts['progress_hooks'] = [progress_cb]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', label) if info else label
            return {'status': 'success', 'message': f'Downloaded: {title}'}

    def download_content(self, url: str, progress_cb=None) -> dict:
        """Main entry point — download content from *url*."""
        plat = self.detect_platform(url)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        folder = os.path.join(DOWNLOAD_DIR, f'{plat}_{ts}')
        os.makedirs(folder, exist_ok=True)

        try:
            if plat == 'youtube':
                return self.download_youtube(url, folder, progress_cb)
            elif plat == 'instagram':
                return self.download_instagram(url, folder, progress_cb)
            else:
                template_map = {
                    'tiktok':   'TikTok_%(uploader)s_%(title)s.%(ext)s',
                    'twitter':  'Twitter_%(uploader)s_%(title)s.%(ext)s',
                    'facebook': 'Facebook_%(title)s.%(ext)s',
                    'reddit':   'Reddit_%(title)s.%(ext)s',
                }
                tmpl = template_map.get(plat, '%(extractor)s_%(title)s.%(ext)s')
                return self._generic_download(url, folder, plat.title(), tmpl, progress_cb)
        except Exception as e:
            return {'status': 'error', 'message': str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# Color palette & design tokens
# ═══════════════════════════════════════════════════════════════════════════════

COLORS = {
    'bg_dark':        '#0F0F0F',
    'bg_card':        '#1A1A1A',
    'bg_input':       '#242424',
    'bg_hover':       '#2A2A2A',
    'accent':         '#F59E0B',      # amber-500
    'accent_hover':   '#D97706',      # amber-600
    'accent_dim':     '#78350F',      # amber-900
    'text_primary':   '#F5F5F5',
    'text_secondary': '#A3A3A3',
    'text_muted':     '#525252',
    'success':        '#22C55E',
    'error':          '#EF4444',
    'border':         '#2E2E2E',
}

FONT_FAMILY = 'Segoe UI'


# ═══════════════════════════════════════════════════════════════════════════════
# Application
# ═══════════════════════════════════════════════════════════════════════════════

class MediaGrabApp(ctk.CTk):
    """Main application window."""

    WIDTH  = 900
    HEIGHT = 640

    def __init__(self):
        super().__init__()

        # ── window chrome ─────────────────────────────────────────────────
        self.title('MediaGrab — Universal Video Downloader')
        self.geometry(f'{self.WIDTH}x{self.HEIGHT}')
        self.minsize(780, 560)
        self.configure(fg_color=COLORS['bg_dark'])

        ctk.set_appearance_mode('dark')
        ctk.set_default_color_theme('dark-blue')

        # ── state ─────────────────────────────────────────────────────────
        self.downloader = UniversalDownloader()
        self._is_downloading = False

        # ── build UI ──────────────────────────────────────────────────────
        self._build_header()
        self._build_tabs()
        self._build_status_bar()

    # ──────────────────────────────────────────────────────────────────────
    # Header
    # ──────────────────────────────────────────────────────────────────────
    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=COLORS['bg_card'], corner_radius=0, height=70)
        header.pack(fill='x')
        header.pack_propagate(False)

        # app icon / title
        title = ctk.CTkLabel(
            header, text='⬇  MediaGrab',
            font=ctk.CTkFont(family=FONT_FAMILY, size=24, weight='bold'),
            text_color=COLORS['accent'],
        )
        title.pack(side='left', padx=24, pady=18)

        subtitle = ctk.CTkLabel(
            header, text='Download videos from 10+ platforms',
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color=COLORS['text_secondary'],
        )
        subtitle.pack(side='left', pady=18)

        # open folder shortcut
        folder_btn = ctk.CTkButton(
            header, text='📂  Open Downloads',
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            fg_color='transparent', hover_color=COLORS['bg_hover'],
            text_color=COLORS['text_secondary'], width=160,
            command=self._open_downloads_folder,
        )
        folder_btn.pack(side='right', padx=20)

    # ──────────────────────────────────────────────────────────────────────
    # Tabs
    # ──────────────────────────────────────────────────────────────────────
    def _build_tabs(self):
        self.tabview = ctk.CTkTabview(
            self,
            fg_color=COLORS['bg_dark'],
            segmented_button_fg_color=COLORS['bg_card'],
            segmented_button_selected_color=COLORS['accent'],
            segmented_button_selected_hover_color=COLORS['accent_hover'],
            segmented_button_unselected_color=COLORS['bg_card'],
            segmented_button_unselected_hover_color=COLORS['bg_hover'],
            text_color=COLORS['text_primary'],
            corner_radius=12,
        )
        self.tabview.pack(fill='both', expand=True, padx=20, pady=(10, 5))

        # create tabs
        tab_single = self.tabview.add('  ⬇  Single Download  ')
        tab_bulk   = self.tabview.add('  📋  Bulk Download  ')
        tab_files  = self.tabview.add('  📁  Downloads  ')

        self._build_single_tab(tab_single)
        self._build_bulk_tab(tab_bulk)
        self._build_files_tab(tab_files)

    # ── Single Download ──────────────────────────────────────────────────
    def _build_single_tab(self, parent):
        wrapper = ctk.CTkFrame(parent, fg_color='transparent')
        wrapper.pack(fill='both', expand=True, padx=10, pady=10)

        # card
        card = ctk.CTkFrame(wrapper, fg_color=COLORS['bg_card'], corner_radius=16)
        card.pack(fill='x', pady=(20, 0))

        inner = ctk.CTkFrame(card, fg_color='transparent')
        inner.pack(fill='x', padx=28, pady=28)

        # ── instruction label
        ctk.CTkLabel(
            inner, text='Paste a video URL to download',
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight='bold'),
            text_color=COLORS['text_primary'],
        ).pack(anchor='w')
        ctk.CTkLabel(
            inner, text='Supports YouTube, Instagram, TikTok, Twitter/X, Facebook, Reddit & more',
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            text_color=COLORS['text_muted'],
        ).pack(anchor='w', pady=(2, 14))

        # ── URL input row
        input_row = ctk.CTkFrame(inner, fg_color='transparent')
        input_row.pack(fill='x')

        self.url_entry = ctk.CTkEntry(
            input_row,
            placeholder_text='https://www.youtube.com/watch?v=...',
            font=ctk.CTkFont(family=FONT_FAMILY, size=14),
            fg_color=COLORS['bg_input'],
            border_color=COLORS['border'],
            text_color=COLORS['text_primary'],
            placeholder_text_color=COLORS['text_muted'],
            height=46, corner_radius=10,
        )
        self.url_entry.pack(side='left', fill='x', expand=True, padx=(0, 10))
        self.url_entry.bind('<KeyRelease>', self._on_url_change)
        self.url_entry.bind('<Return>', lambda e: self._start_single_download())

        self.download_btn = ctk.CTkButton(
            input_row, text='Download',
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight='bold'),
            fg_color=COLORS['accent'], hover_color=COLORS['accent_hover'],
            text_color='#000000', width=130, height=46, corner_radius=10,
            command=self._start_single_download,
        )
        self.download_btn.pack(side='right')

        # ── platform badge
        self.platform_badge = ctk.CTkLabel(
            inner, text='',
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color=COLORS['accent'],
            fg_color=COLORS['accent_dim'],
            corner_radius=8, height=30,
        )
        # hidden initially

        # ── progress bar
        self.progress_bar = ctk.CTkProgressBar(
            inner,
            fg_color=COLORS['bg_input'],
            progress_color=COLORS['accent'],
            height=6, corner_radius=3,
        )
        self.progress_bar.set(0)
        # hidden initially

        # ── result message area
        self.result_label = ctk.CTkLabel(
            inner, text='',
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color=COLORS['text_secondary'],
            wraplength=700, justify='left',
        )

        # ── supported platforms showcase
        platforms_card = ctk.CTkFrame(wrapper, fg_color=COLORS['bg_card'], corner_radius=16)
        platforms_card.pack(fill='x', pady=(16, 0))

        plat_inner = ctk.CTkFrame(platforms_card, fg_color='transparent')
        plat_inner.pack(fill='x', padx=28, pady=20)

        ctk.CTkLabel(
            plat_inner, text='Supported Platforms',
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight='bold'),
            text_color=COLORS['text_primary'],
        ).pack(anchor='w', pady=(0, 10))

        badges_frame = ctk.CTkFrame(plat_inner, fg_color='transparent')
        badges_frame.pack(fill='x')

        platforms_display = [
            ('▶  YouTube', '#FF0000'),
            ('📷  Instagram', '#E1306C'),
            ('🎵  TikTok', '#00F2EA'),
            ('🐦  Twitter / X', '#1DA1F2'),
            ('📘  Facebook', '#1877F2'),
            ('🤖  Reddit', '#FF4500'),
            ('🎮  Twitch', '#9146FF'),
            ('📌  Pinterest', '#E60023'),
        ]
        for i, (label, color) in enumerate(platforms_display):
            badge = ctk.CTkLabel(
                badges_frame, text=f'  {label}  ',
                font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                text_color=color,
                fg_color=COLORS['bg_input'],
                corner_radius=8, height=32,
            )
            badge.grid(row=i // 4, column=i % 4, padx=4, pady=4, sticky='ew')

        for c in range(4):
            badges_frame.columnconfigure(c, weight=1)

    # ── Bulk Download ────────────────────────────────────────────────────
    def _build_bulk_tab(self, parent):
        wrapper = ctk.CTkFrame(parent, fg_color='transparent')
        wrapper.pack(fill='both', expand=True, padx=10, pady=10)

        card = ctk.CTkFrame(wrapper, fg_color=COLORS['bg_card'], corner_radius=16)
        card.pack(fill='both', expand=True, pady=(20, 0))

        inner = ctk.CTkFrame(card, fg_color='transparent')
        inner.pack(fill='both', expand=True, padx=28, pady=28)

        ctk.CTkLabel(
            inner, text='Paste multiple URLs (one per line)',
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight='bold'),
            text_color=COLORS['text_primary'],
        ).pack(anchor='w')
        ctk.CTkLabel(
            inner, text='Each URL will be downloaded sequentially',
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            text_color=COLORS['text_muted'],
        ).pack(anchor='w', pady=(2, 14))

        self.bulk_text = ctk.CTkTextbox(
            inner,
            font=ctk.CTkFont(family='Consolas', size=13),
            fg_color=COLORS['bg_input'],
            text_color=COLORS['text_primary'],
            border_color=COLORS['border'],
            border_width=1, corner_radius=10,
        )
        self.bulk_text.pack(fill='both', expand=True, pady=(0, 14))

        btn_row = ctk.CTkFrame(inner, fg_color='transparent')
        btn_row.pack(fill='x')

        self.bulk_counter = ctk.CTkLabel(
            btn_row, text='0 URLs',
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color=COLORS['text_muted'],
        )
        self.bulk_counter.pack(side='left')

        self.bulk_progress_label = ctk.CTkLabel(
            btn_row, text='',
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color=COLORS['accent'],
        )
        self.bulk_progress_label.pack(side='left', padx=20)

        self.bulk_btn = ctk.CTkButton(
            btn_row, text='Download All',
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight='bold'),
            fg_color=COLORS['accent'], hover_color=COLORS['accent_hover'],
            text_color='#000000', width=150, height=42, corner_radius=10,
            command=self._start_bulk_download,
        )
        self.bulk_btn.pack(side='right')

        self.bulk_text.bind('<KeyRelease>', self._on_bulk_text_change)

    # ── Downloads Viewer ─────────────────────────────────────────────────
    def _build_files_tab(self, parent):
        wrapper = ctk.CTkFrame(parent, fg_color='transparent')
        wrapper.pack(fill='both', expand=True, padx=10, pady=10)

        # top bar
        top_bar = ctk.CTkFrame(wrapper, fg_color='transparent')
        top_bar.pack(fill='x', pady=(10, 6))

        ctk.CTkLabel(
            top_bar, text='Downloaded Files',
            font=ctk.CTkFont(family=FONT_FAMILY, size=16, weight='bold'),
            text_color=COLORS['text_primary'],
        ).pack(side='left')

        refresh_btn = ctk.CTkButton(
            top_bar, text='🔄  Refresh',
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            fg_color=COLORS['bg_card'], hover_color=COLORS['bg_hover'],
            text_color=COLORS['text_secondary'], width=110, height=36, corner_radius=8,
            command=self._refresh_files,
        )
        refresh_btn.pack(side='right', padx=(6, 0))

        open_btn = ctk.CTkButton(
            top_bar, text='📂  Open Folder',
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            fg_color=COLORS['bg_card'], hover_color=COLORS['bg_hover'],
            text_color=COLORS['text_secondary'], width=130, height=36, corner_radius=8,
            command=self._open_downloads_folder,
        )
        open_btn.pack(side='right', padx=(6, 0))

        clear_btn = ctk.CTkButton(
            top_bar, text='🗑  Clear All',
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            fg_color=COLORS['bg_card'], hover_color='#7F1D1D',
            text_color=COLORS['error'], width=110, height=36, corner_radius=8,
            command=self._clear_downloads,
        )
        clear_btn.pack(side='right')

        # scrollable list
        self.files_scroll = ctk.CTkScrollableFrame(
            wrapper,
            fg_color=COLORS['bg_card'],
            corner_radius=12,
            scrollbar_button_color=COLORS['bg_hover'],
            scrollbar_button_hover_color=COLORS['text_muted'],
        )
        self.files_scroll.pack(fill='both', expand=True, pady=(4, 0))

        self.files_empty_label = ctk.CTkLabel(
            self.files_scroll, text='No downloads yet.\nPaste a URL and hit Download!',
            font=ctk.CTkFont(family=FONT_FAMILY, size=14),
            text_color=COLORS['text_muted'], justify='center',
        )
        self.files_empty_label.pack(pady=60)

        # initial load
        self._refresh_files()

    # ──────────────────────────────────────────────────────────────────────
    # Status bar
    # ──────────────────────────────────────────────────────────────────────
    def _build_status_bar(self):
        bar = ctk.CTkFrame(self, fg_color=COLORS['bg_card'], corner_radius=0, height=32)
        bar.pack(fill='x', side='bottom')
        bar.pack_propagate(False)

        self.status_label = ctk.CTkLabel(
            bar, text='Ready',
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            text_color=COLORS['text_muted'],
        )
        self.status_label.pack(side='left', padx=16)

        path_label = ctk.CTkLabel(
            bar, text=f'📁 {DOWNLOAD_DIR}',
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLORS['text_muted'],
        )
        path_label.pack(side='right', padx=16)

    # ──────────────────────────────────────────────────────────────────────
    # Event handlers
    # ──────────────────────────────────────────────────────────────────────
    def _on_url_change(self, event=None):
        url = self.url_entry.get().strip()
        if url:
            plat = self.downloader.detect_platform(url)
            label = UniversalDownloader.PLATFORM_LABELS.get(plat, '🌐  Other')
            self.platform_badge.configure(text=f'  {label}  ')
            self.platform_badge.pack(anchor='w', pady=(10, 0))
        else:
            self.platform_badge.pack_forget()

    def _on_bulk_text_change(self, event=None):
        text = self.bulk_text.get('1.0', 'end').strip()
        count = len([u for u in text.splitlines() if u.strip()])
        self.bulk_counter.configure(text=f'{count} URL{"s" if count != 1 else ""}')

    # ──────────────────────────────────────────────────────────────────────
    # Download logic
    # ──────────────────────────────────────────────────────────────────────
    def _set_downloading(self, state: bool):
        self._is_downloading = state
        btn_state = 'disabled' if state else 'normal'
        self.download_btn.configure(state=btn_state)
        self.bulk_btn.configure(state=btn_state)
        if state:
            self.progress_bar.pack(fill='x', pady=(14, 0))
            self.progress_bar.set(0)
            self.progress_bar.configure(mode='indeterminate')
            self.progress_bar.start()
        else:
            self.progress_bar.stop()
            self.progress_bar.pack_forget()

    def _set_status(self, text: str, color: str = COLORS['text_muted']):
        self.status_label.configure(text=text, text_color=color)

    def _show_result(self, msg: str, is_error: bool = False):
        color = COLORS['error'] if is_error else COLORS['success']
        self.result_label.configure(text=msg, text_color=color)
        self.result_label.pack(anchor='w', pady=(12, 0))

    def _progress_hook(self, d):
        if d.get('status') == 'downloading':
            pct = d.get('_percent_str', '').strip()
            speed = d.get('_speed_str', '').strip()
            self._set_status(f'Downloading… {pct}  ({speed})', COLORS['accent'])
        elif d.get('status') == 'finished':
            self._set_status('Merging / finalising…', COLORS['accent'])

    def _start_single_download(self):
        url = self.url_entry.get().strip()
        if not url:
            self._show_result('Please enter a URL.', is_error=True)
            return
        if self._is_downloading:
            return

        self.result_label.pack_forget()
        self._set_downloading(True)
        self._set_status('Starting download…', COLORS['accent'])

        def worker():
            try:
                result = self.downloader.download_content(url, progress_cb=self._progress_hook)
                is_err = result.get('status') != 'success'
                msg = result.get('message', 'Done')
                self.after(0, lambda: self._show_result(msg, is_err))
                status_text = 'Download failed' if is_err else 'Download complete ✓'
                status_color = COLORS['error'] if is_err else COLORS['success']
                self.after(0, lambda: self._set_status(status_text, status_color))
            except Exception as e:
                self.after(0, lambda: self._show_result(f'Error: {e}', True))
                self.after(0, lambda: self._set_status('Error', COLORS['error']))
            finally:
                self.after(0, lambda: self._set_downloading(False))
                self.after(0, self._refresh_files)

        threading.Thread(target=worker, daemon=True).start()

    def _start_bulk_download(self):
        text = self.bulk_text.get('1.0', 'end').strip()
        urls = [u.strip() for u in text.splitlines() if u.strip()]
        if not urls:
            self.bulk_progress_label.configure(text='No URLs entered.', text_color=COLORS['error'])
            return
        if self._is_downloading:
            return

        self._set_downloading(True)
        self._set_status('Bulk download starting…', COLORS['accent'])

        def worker():
            total = len(urls)
            success = 0
            for i, url in enumerate(urls, 1):
                self.after(0, lambda i=i, t=total: self.bulk_progress_label.configure(
                    text=f'Downloading {i}/{t}…', text_color=COLORS['accent']))
                self.after(0, lambda i=i, t=total: self._set_status(
                    f'Bulk: {i}/{t}', COLORS['accent']))
                try:
                    r = self.downloader.download_content(url, progress_cb=self._progress_hook)
                    if r.get('status') == 'success':
                        success += 1
                except Exception:
                    pass

            msg = f'Done — {success}/{total} downloaded successfully'
            self.after(0, lambda: self.bulk_progress_label.configure(
                text=msg, text_color=COLORS['success']))
            self.after(0, lambda: self._set_status(msg, COLORS['success']))
            self.after(0, lambda: self._set_downloading(False))
            self.after(0, self._refresh_files)

        threading.Thread(target=worker, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────────
    # Downloads viewer
    # ──────────────────────────────────────────────────────────────────────
    def _refresh_files(self):
        for widget in self.files_scroll.winfo_children():
            widget.destroy()

        if not os.path.exists(DOWNLOAD_DIR):
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        items = sorted(os.listdir(DOWNLOAD_DIR), reverse=True)
        if not items:
            self.files_empty_label = ctk.CTkLabel(
                self.files_scroll, text='No downloads yet.\nPaste a URL and hit Download!',
                font=ctk.CTkFont(family=FONT_FAMILY, size=14),
                text_color=COLORS['text_muted'], justify='center',
            )
            self.files_empty_label.pack(pady=60)
            return

        for item_name in items:
            item_path = os.path.join(DOWNLOAD_DIR, item_name)
            is_dir = os.path.isdir(item_path)

            row = ctk.CTkFrame(self.files_scroll, fg_color=COLORS['bg_input'], corner_radius=10, height=48)
            row.pack(fill='x', pady=3, padx=4)
            row.pack_propagate(False)

            icon = '📂' if is_dir else '🎬'
            if is_dir:
                file_count = len([f for f in os.listdir(item_path) if os.path.isfile(os.path.join(item_path, f))])
                detail = f'{file_count} file{"s" if file_count != 1 else ""}'
            else:
                size_mb = os.path.getsize(item_path) / (1024 * 1024)
                detail = f'{size_mb:.1f} MB'

            ctk.CTkLabel(
                row, text=f'{icon}  {item_name}',
                font=ctk.CTkFont(family=FONT_FAMILY, size=13),
                text_color=COLORS['text_primary'],
            ).pack(side='left', padx=14)

            ctk.CTkLabel(
                row, text=detail,
                font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                text_color=COLORS['text_muted'],
            ).pack(side='left', padx=(4, 0))

            open_cmd = (lambda p=item_path: self._open_path(p))
            ctk.CTkButton(
                row, text='Open',
                font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                fg_color='transparent', hover_color=COLORS['bg_hover'],
                text_color=COLORS['accent'], width=60, height=30, corner_radius=6,
                command=open_cmd,
            ).pack(side='right', padx=10)

    def _clear_downloads(self):
        """Delete all downloads after confirmation."""
        import shutil
        if os.path.exists(DOWNLOAD_DIR):
            shutil.rmtree(DOWNLOAD_DIR)
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self._refresh_files()
        self._set_status('Downloads cleared', COLORS['text_muted'])

    # ──────────────────────────────────────────────────────────────────────
    # OS helpers
    # ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def _open_path(path: str):
        if platform.system() == 'Windows':
            os.startfile(path)
        elif platform.system() == 'Darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])

    def _open_downloads_folder(self):
        self._open_path(DOWNLOAD_DIR)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    app = MediaGrabApp()
    app.mainloop()
