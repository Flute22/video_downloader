from flask import Flask, request, render_template, jsonify, send_file, send_from_directory, Response
from flask_cors import CORS
import os
import tempfile
import requests
import re
from datetime import datetime
import yt_dlp
import instaloader
from werkzeug.utils import secure_filename
import zipfile
import shutil
import queue
import json

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['SECRET_KEY'] = 'your-secret-key-here-change-this'

# Global dictionary to hold progress queues for each client
progress_queues = {}

# Use /tmp on Render (no persistent disk on free tier), local dir otherwise
IS_RENDER = os.environ.get('RENDER', False)
if IS_RENDER:
    DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), 'anyvideo_downloads')
else:
    DOWNLOAD_DIR = os.path.join(os.getcwd(), 'downloads')

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# Server-side cookies file for bypassing YouTube bot checks
if IS_RENDER:
    SERVER_COOKIES_PATH = os.path.join(tempfile.gettempdir(), 'anyvideo_cookies.txt')
else:
    SERVER_COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

# Check if cookies are provided via Environment Variable (e.g. on Render)
ENV_COOKIES = os.environ.get('YOUTUBE_COOKIES', '').strip()
if ENV_COOKIES:
    try:
        # Handle all possible newline encodings from env vars
        cookie_text = ENV_COOKIES
        # If Render stored literal \n (backslash + n), convert to real newlines
        if '\\n' in cookie_text and '\n' not in cookie_text:
            cookie_text = cookie_text.replace('\\n', '\n')
        # Also handle \\t -> real tabs (in case tabs got escaped too)
        if '\\t' in cookie_text and '\t' not in cookie_text:
            cookie_text = cookie_text.replace('\\t', '\t')
        
        with open(SERVER_COOKIES_PATH, 'w', encoding='utf-8') as f:
            f.write(cookie_text)
        
        # Debug: verify the file was written correctly
        with open(SERVER_COOKIES_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        print(f"Loaded YouTube cookies from environment variable.")
        print(f"  Cookie file: {SERVER_COOKIES_PATH}")
        print(f"  Total lines: {len(lines)}")
        print(f"  First line: {lines[0].strip()[:60] if lines else 'EMPTY'}")
        print(f"  Has tabs: {any(chr(9) in line for line in lines)}")
        print(f"  Non-comment lines: {sum(1 for l in lines if l.strip() and not l.startswith('#'))}")
    except Exception as e:
        print(f"Failed to write environment cookies: {e}")

# Optional Proxy
PROXY_URL = os.environ.get('PROXY_URL', '')

# Path to ffmpeg for merging video+audio
FFMPEG_PATH = shutil.which('ffmpeg')
if not FFMPEG_PATH:
    FFMPEG_PATH = os.path.expanduser('~/AppData/Local/Microsoft/WinGet/Links')

class UniversalDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        })
        
    def detect_platform(self, url):
        """Detect the platform from URL"""
        url = url.lower()
        if 'youtube.com' in url or 'youtu.be' in url:
            return 'youtube'
        elif 'instagram.com' in url:
            return 'instagram'
        elif 'facebook.com' in url or 'fb.watch' in url:
            return 'facebook'
        elif 'twitter.com' in url or 'x.com' in url:
            return 'twitter'
        elif 'tiktok.com' in url:
            return 'tiktok'
        elif 'pinterest.com' in url:
            return 'pinterest'
        elif 'linkedin.com' in url:
            return 'linkedin'
        elif 'snapchat.com' in url:
            return 'snapchat'
        elif 'reddit.com' in url:
            return 'reddit'
        elif 'twitch.tv' in url:
            return 'twitch'
        else:
            return 'unknown'
    
    def create_safe_filename(self, filename, max_length=100):
        """Create a safe filename"""
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = filename.strip()
        if len(filename) > max_length:
            filename = filename[:max_length]
        return filename

    def _create_progress_hook(self, client_id):
        """Create a yt-dlp progress hook that pushes to the client's queue"""
        def progress_hook(d):
            if not client_id or client_id not in progress_queues:
                return
            
            # Remove ANSI escape codes yt-dlp might use for colors
            def clean_str(s):
                return re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', str(s)).strip()
            
            if d['status'] == 'downloading':
                progress_queues[client_id].put({
                    'status': 'downloading',
                    'percent': clean_str(d.get('_percent_str', '0%')),
                    'speed': clean_str(d.get('_speed_str', 'Unknown')),
                    'eta': clean_str(d.get('_eta_str', 'Unknown')),
                    'filename': os.path.basename(d.get('filename', ''))
                })
            elif d['status'] == 'finished':
                progress_queues[client_id].put({
                    'status': 'processing',
                    'percent': '100%',
                    'speed': 'Processing/Merging...',
                    'eta': '00:00'
                })
        return progress_hook

    def _push_status(self, client_id, status_msg):
        """Helper to push a generic status to the client queue"""
        if client_id and client_id in progress_queues:
            progress_queues[client_id].put({
                'status': 'downloading',
                'percent': '...',
                'speed': status_msg,
                'eta': '...'
            })

    _QUALITY_FORMATS = {
        'Best Available': [
            'bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1]+bestaudio/bestvideo+bestaudio[acodec^=mp4a]/bestvideo+bestaudio/best',
            'bestvideo+bestaudio/best',
            'best',
        ],
        '1080p (Full HD)': [
            'bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=1080][vcodec^=avc1]+bestaudio/bestvideo[height<=1080]+bestaudio[acodec^=mp4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
            'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
            'best',
        ],
        '720p (HD)': [
            'bestvideo[height<=720][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=720][vcodec^=avc1]+bestaudio/bestvideo[height<=720]+bestaudio[acodec^=mp4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best',
            'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
            'best',
        ],
        '480p': [
            'bestvideo[height<=480][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=480][vcodec^=avc1]+bestaudio/bestvideo[height<=480]+bestaudio[acodec^=mp4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best',
            'bestvideo[height<=480]+bestaudio/best[height<=480]/best',
            'best',
        ],
    }

    def _youtube_subtitle_opts(self):
        """Download subtitles when available, including auto captions."""
        return {
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en', 'en-US', 'hi', 'all'],
            'subtitlesformat': 'srt/best',
            'postprocessors': [
                {'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'}
            ],
        }

    @staticmethod
    def _count_subtitle_files(path):
        subtitle_exts = {'.srt', '.vtt', '.ass', '.lrc'}
        total = 0
        for root, _, files in os.walk(path):
            for name in files:
                if os.path.splitext(name)[1].lower() in subtitle_exts:
                    total += 1
        return total
    
    def download_youtube_content(self, url, path, client_id=None, cookies_path=None,
                                 quality='Best Available', subtitles=False):
        """Download YouTube videos, shorts, playlists"""
        import time
        
        # Format strings to try in order of preference (fallback chain)
        format_attempts = self._QUALITY_FORMATS.get(
            quality, self._QUALITY_FORMATS['Best Available']
        )
        
        last_error = None
        
        for attempt_idx, fmt in enumerate(format_attempts):
            try:
                if attempt_idx > 0:
                    self._push_status(client_id, f'Retrying with format fallback ({attempt_idx + 1}/{len(format_attempts)})...')
                    # Sleep between retries to avoid 429
                    time.sleep(3 + attempt_idx * 2)
                
                ydl_opts = {
                    'outtmpl': os.path.join(path, '%(uploader)s - %(title)s.%(ext)s'),
                    'format': fmt,
                    'merge_output_format': 'mp4',
                    'ffmpeg_location': FFMPEG_PATH,
                    'ignoreerrors': True,
                    'retries': 10,
                    'fragment_retries': 10,
                    'file_access_retries': 5,
                    'extractor_retries': 5,
                    'retry_sleep_functions': {'http': lambda n: 2 + n * 2},
                    'socket_timeout': 60,
                    'noprogress': True,  # We use hooks instead of stdout
                    'quiet': False,
                    'no_warnings': False,
                    'geo_bypass': True,
                    'cachedir': False,
                    'force_ipv4': True,
                    # Sleep between requests to avoid YouTube 429 rate-limiting
                    'sleep_interval': 1,
                    'max_sleep_interval': 5,
                    'sleep_interval_requests': 1,
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Referer': 'https://www.youtube.com/',
                    },
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['default', 'web'],
                        }
                    },
                    # FFmpeg reconnection for 403 errors on stream fragments
                    'downloader_args': {
                        'ffmpeg_i': ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5']
                    },
                    # Convert audio to AAC for player compatibility (e.g. Films & TV)
                    'postprocessor_args': {
                        'ffmpeg': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k']
                    },
                }
                if subtitles:
                    ydl_opts.update(self._youtube_subtitle_opts())
                if cookies_path:
                    ydl_opts['cookiefile'] = cookies_path
                    
                if PROXY_URL:
                    ydl_opts['proxy'] = PROXY_URL
                
                if client_id:
                    ydl_opts['progress_hooks'] = [self._create_progress_hook(client_id)]
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    
                    if info is None:
                        last_error = 'Could not extract video info. The video may be private, age-restricted, or unavailable.'
                        continue  # Try next format
                    
                    if 'entries' in info:  # Playlist
                        titles = [entry.get('title', 'Unknown') for entry in info['entries'] if entry]
                        subtitle_count = self._count_subtitle_files(path) if subtitles else 0
                        subtitle_note = f' Subtitles saved: {subtitle_count}.' if subtitle_count else ''
                        return {
                            'status': 'success',
                            'message': f'Downloaded {len(titles)} videos from playlist.{subtitle_note}',
                            'titles': titles[:5],  # Show first 5 titles
                            'type': 'playlist'
                        }
                    else:  # Single video
                        subtitle_count = self._count_subtitle_files(path) if subtitles else 0
                        subtitle_note = f' Subtitles saved: {subtitle_count}.' if subtitle_count else ''
                        return {
                            'status': 'success',
                            'message': f'YouTube content downloaded successfully!{subtitle_note}',
                            'title': info.get('title', 'Unknown'),
                            'uploader': info.get('uploader', 'Unknown'),
                            'type': 'video'
                        }
            except yt_dlp.utils.DownloadError as e:
                last_error = str(e)
                print(f"YouTube attempt {attempt_idx + 1} failed (format='{fmt}'): {last_error}")
                # If it's a format error, try next format; otherwise break
                if 'Requested format' in last_error or 'No video formats' in last_error or 'Only images' in last_error:
                    continue
                else:
                    break
            except Exception as e:
                last_error = str(e)
                print(f"YouTube attempt {attempt_idx + 1} unexpected error: {last_error}")
                break
        
        return {'status': 'error', 'message': f'YouTube error: {last_error}'}
    
    def download_instagram_content(self, url, path, client_id=None):
        """Download Instagram posts, reels, stories, IGTV"""
        try:
            loader = instaloader.Instaloader(
                dirname_pattern=path,
                filename_pattern='{profile}_{mediaid}_{date_utc}',
                download_videos=True,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=True,
                compress_json=False
            )
            
            self._push_status(client_id, "Fetching Instagram data...")
            
            # Handle different Instagram URL types
            if '/stories/' in url:
                # Story URL
                username = self.extract_instagram_username(url)
                if username:
                    profile = instaloader.Profile.from_username(loader.context, username)
                    self._push_status(client_id, f"Downloading stories for {username}...")
                    for story in loader.get_stories([profile.userid]):
                        for item in story.get_items():
                            loader.download_storyitem(item, target=username)
                    return {
                        'status': 'success',
                        'message': f'Instagram stories downloaded for {username}',
                        'type': 'stories'
                    }
            elif '/reel/' in url or '/p/' in url or '/tv/' in url:
                # Post, Reel, or IGTV
                shortcode = self.extract_instagram_shortcode(url)
                post = instaloader.Post.from_shortcode(loader.context, shortcode)
                
                self._push_status(client_id, f"Downloading {post.owner_username}'s post...")
                loader.download_post(post, target=post.owner_username)
                
                content_type = 'reel' if post.is_video else 'post'
                if post.typename == 'GraphSidecar':
                    content_type = 'carousel'
                
                return {
                    'status': 'success',
                    'message': f'Instagram {content_type} downloaded successfully!',
                    'username': post.owner_username,
                    'caption': post.caption[:100] + '...' if post.caption and len(post.caption) > 100 else post.caption,
                    'type': content_type
                }
            else:
                # Profile URL - download recent posts
                username = self.extract_instagram_username(url)
                profile = instaloader.Profile.from_username(loader.context, username)
                
                count = 0
                for post in profile.get_posts():
                    if count >= 10:  # Limit to 10 recent posts
                        break
                    self._push_status(client_id, f"Downloading post {count+1}/10...")
                    loader.download_post(post, target=username)
                    count += 1
                
                return {
                    'status': 'success',
                    'message': f'Downloaded {count} recent posts from {username}',
                    'type': 'profile'
                }
                
        except Exception as e:
            return {'status': 'error', 'message': f'Instagram error: {str(e)}'}
    
    def download_tiktok_content(self, url, path, client_id=None, cookies_path=None):
        """Download TikTok videos"""
        try:
            ydl_opts = {
                'outtmpl': os.path.join(path, 'TikTok_%(uploader)s_%(title)s.%(ext)s'),
                'format': 'best',
                'merge_output_format': 'mp4',
                'ffmpeg_location': FFMPEG_PATH,
                'quiet': True,
                'noprogress': True,
            }
            if cookies_path:
                ydl_opts['cookiefile'] = cookies_path
            if client_id:
                ydl_opts['progress_hooks'] = [self._create_progress_hook(client_id)]
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'status': 'success',
                    'message': 'TikTok video downloaded successfully!',
                    'title': info.get('title', 'TikTok Video'),
                    'uploader': info.get('uploader', 'Unknown'),
                    'type': 'video'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'TikTok error: {str(e)}'}
    
    def download_twitter_content(self, url, path, client_id=None, cookies_path=None):
        """Download Twitter/X videos, images, threads"""
        try:
            ydl_opts = {
                'outtmpl': os.path.join(path, 'Twitter_%(uploader)s_%(title)s.%(ext)s'),
                'format': 'best',
                'merge_output_format': 'mp4',
                'ffmpeg_location': FFMPEG_PATH,
                'quiet': True,
                'noprogress': True,
            }
            if cookies_path:
                ydl_opts['cookiefile'] = cookies_path
            if client_id:
                ydl_opts['progress_hooks'] = [self._create_progress_hook(client_id)]
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'status': 'success',
                    'message': 'Twitter content downloaded successfully!',
                    'title': info.get('title', 'Twitter Content'),
                    'uploader': info.get('uploader', 'Unknown'),
                    'type': 'tweet'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Twitter error: {str(e)}'}
    
    def download_facebook_content(self, url, path, client_id=None, cookies_path=None):
        """Download Facebook videos, posts"""
        try:
            ydl_opts = {
                'outtmpl': os.path.join(path, 'Facebook_%(title)s.%(ext)s'),
                'format': 'best',
                'merge_output_format': 'mp4',
                'ffmpeg_location': FFMPEG_PATH,
                'quiet': True,
                'noprogress': True,
            }
            if cookies_path:
                ydl_opts['cookiefile'] = cookies_path
            if client_id:
                ydl_opts['progress_hooks'] = [self._create_progress_hook(client_id)]
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'status': 'success',
                    'message': 'Facebook content downloaded successfully!',
                    'title': info.get('title', 'Facebook Content'),
                    'type': 'video'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Facebook error: {str(e)}'}
    
    def download_reddit_content(self, url, path, client_id=None, cookies_path=None):
        """Download Reddit videos, images, gifs"""
        try:
            ydl_opts = {
                'outtmpl': os.path.join(path, 'Reddit_%(title)s.%(ext)s'),
                'format': 'best',
                'merge_output_format': 'mp4',
                'ffmpeg_location': FFMPEG_PATH,
                'quiet': True,
                'noprogress': True,
            }
            if cookies_path:
                ydl_opts['cookiefile'] = cookies_path
            if client_id:
                ydl_opts['progress_hooks'] = [self._create_progress_hook(client_id)]
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'status': 'success',
                    'message': 'Reddit content downloaded successfully!',
                    'title': info.get('title', 'Reddit Post'),
                    'type': 'post'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Reddit error: {str(e)}'}
    
    def download_generic_content(self, url, path, client_id=None, cookies_path=None):
        """Download from any supported platform using yt-dlp"""
        try:
            ydl_opts = {
                'outtmpl': os.path.join(path, '%(extractor)s_%(title)s.%(ext)s'),
                'format': 'best',
                'merge_output_format': 'mp4',
                'ffmpeg_location': FFMPEG_PATH,
                'quiet': True,
                'noprogress': True,
            }
            if cookies_path:
                ydl_opts['cookiefile'] = cookies_path
            if client_id:
                ydl_opts['progress_hooks'] = [self._create_progress_hook(client_id)]
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'status': 'success',
                    'message': 'Content downloaded successfully!',
                    'title': info.get('title', 'Unknown'),
                    'extractor': info.get('extractor', 'Unknown'),
                    'type': 'media'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Download error: {str(e)}'}
    
    def extract_instagram_shortcode(self, url):
        """Extract shortcode from Instagram URL"""
        patterns = [
            r'/p/([^/?]+)',
            r'/reel/([^/?]+)',
            r'/tv/([^/?]+)'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None
    
    def extract_instagram_username(self, url):
        """Extract username from Instagram URL"""
        match = re.search(r'instagram\.com/([^/?]+)', url)
        if match:
            return match.group(1)
        return None
    
    def download_content(self, url, custom_path=None, client_id=None, cookies_path=None,
                         quality='Best Available', subtitles=False):
        """Main download function"""
        path = custom_path or DOWNLOAD_DIR
        platform = self.detect_platform(url)
        
        # Create timestamped folder for this download
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        download_folder = os.path.join(path, f"{platform}_{timestamp}")
        os.makedirs(download_folder, exist_ok=True)
        
        try:
            if platform == 'youtube':
                return self.download_youtube_content(url, download_folder, client_id, cookies_path, quality, subtitles)
            elif platform == 'instagram':
                return self.download_instagram_content(url, download_folder, client_id)
            elif platform == 'tiktok':
                return self.download_tiktok_content(url, download_folder, client_id, cookies_path)
            elif platform == 'twitter':
                return self.download_twitter_content(url, download_folder, client_id, cookies_path)
            elif platform == 'facebook':
                return self.download_facebook_content(url, download_folder, client_id, cookies_path)
            elif platform == 'reddit':
                return self.download_reddit_content(url, download_folder, client_id, cookies_path)
            else:
                # Try generic download for other platforms
                return self.download_generic_content(url, download_folder, client_id, cookies_path)
                
        except Exception as e:
            return {'status': 'error', 'message': f'Unexpected error: {str(e)}'}

# Initialize downloader
downloader = UniversalDownloader()

@app.route('/')
def index():
    """Main page - serves the website landing page with working downloader"""
    return send_from_directory('website', 'index.html')

@app.route('/<path:filename>')
def website_static(filename):
    """Serve static files from the website directory (e.g. .exe, .dmg downloads)"""
    return send_from_directory('website', filename)

@app.route('/app')
def app_ui():
    """Full app UI page"""
    return render_template('index.html')

@app.route('/stream-progress')
def stream_progress():
    """Server-Sent Events endpoint for real-time download progress"""
    client_id = request.args.get('client_id')
    if not client_id:
        return jsonify({'error': 'Missing client_id'}), 400

    # Initialize queue for this client if it doesn't exist
    if client_id not in progress_queues:
        progress_queues[client_id] = queue.Queue()

    def generate():
        q = progress_queues[client_id]
        try:
            while True:
                # Block until a message is available
                message = q.get(timeout=30)
                if message is None: # None means close connection
                    yield "event: close\ndata: \n\n"
                    break
                
                # Send SSE data
                yield f"data: {json.dumps(message)}\n\n"
        except queue.Empty:
            # Keep-alive or timeout
            pass
        except Exception as e:
            print(f"SSE Error: {e}")
        finally:
            if client_id in progress_queues:
                del progress_queues[client_id]
                
    return Response(generate(), mimetype='text/event-stream')

@app.route('/download', methods=['POST'])
def download():
    """Handle download requests"""
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        client_id = data.get('client_id')
        quality = data.get('quality', 'Best Available')
        subtitles = data.get('subtitles', False)
        
        if not url:
            return jsonify({'status': 'error', 'message': 'URL is required'})
            
        cookies_path = SERVER_COOKIES_PATH if os.path.exists(SERVER_COOKIES_PATH) else None
        print(f"Debug: Using cookies_path = {cookies_path}")
        
        # Detect platform automatically
        platform = downloader.detect_platform(url)
        
        # Start download
        result = downloader.download_content(
            url, client_id=client_id, cookies_path=cookies_path,
            quality=quality, subtitles=subtitles
        )
        result['platform'] = platform
        
        # Signal the SSE stream to close
        if client_id and client_id in progress_queues:
            progress_queues[client_id].put(None)
            
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Server error: {str(e)}'})

@app.route('/bulk-download', methods=['POST'])
def bulk_download():
    """Handle bulk download requests"""
    try:
        data = request.get_json()
        urls = data.get('urls', [])
        client_id = data.get('client_id')
        quality = data.get('quality', 'Best Available')
        subtitles = data.get('subtitles', False)
        
        if not urls:
            return jsonify({'status': 'error', 'message': 'URLs list is required'})
            
        cookies_path = SERVER_COOKIES_PATH if os.path.exists(SERVER_COOKIES_PATH) else None
        
        results = []
        for i, url in enumerate(urls):
            if url.strip():
                if client_id and client_id in progress_queues:
                    progress_queues[client_id].put({
                        'status': 'downloading',
                        'percent': '...',
                        'speed': f'File {i+1} of {len(urls)}',
                        'eta': '...'
                    })
                
                result = downloader.download_content(
                    url.strip(), client_id=client_id, cookies_path=cookies_path,
                    quality=quality, subtitles=subtitles
                )
                result['url'] = url
                results.append(result)
        
        if client_id and client_id in progress_queues:
            progress_queues[client_id].put(None)
            
        return jsonify({
            'status': 'success',
            'message': f'Processed {len(results)} URLs',
            'results': results
        })
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Bulk download error: {str(e)}'})

@app.route('/downloads')
def list_downloads():
    """List downloaded files and folders"""
    try:
        items = []
        if os.path.exists(DOWNLOAD_DIR):
            for item in os.listdir(DOWNLOAD_DIR):
                item_path = os.path.join(DOWNLOAD_DIR, item)
                if os.path.isfile(item_path):
                    items.append({
                        'name': item,
                        'type': 'file',
                        'size': os.path.getsize(item_path)
                    })
                elif os.path.isdir(item_path):
                    file_count = len([f for f in os.listdir(item_path) if os.path.isfile(os.path.join(item_path, f))])
                    items.append({
                        'name': item,
                        'type': 'folder',
                        'file_count': file_count
                    })
        
        return jsonify({'items': items})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/download-file/<path:filename>')
def download_file(filename):
    """Download a specific file"""
    try:
        safe_filename = secure_filename(filename)
        file_path = os.path.join(DOWNLOAD_DIR, safe_filename)
        
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True)
        else:
            return jsonify({'error': 'File not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download-folder/<foldername>')
def download_folder(foldername):
    """Download a folder as ZIP"""
    try:
        safe_foldername = secure_filename(foldername)
        folder_path = os.path.join(DOWNLOAD_DIR, safe_foldername)
        
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            # Create a temporary ZIP file
            temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            temp_zip.close()
            
            with zipfile.ZipFile(temp_zip.name, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(folder_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, folder_path)
                        zipf.write(file_path, arcname)
            
            return send_file(temp_zip.name, as_attachment=True, download_name=f'{safe_foldername}.zip')
        else:
            return jsonify({'error': 'Folder not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/supported-platforms')
def supported_platforms():
    """List supported platforms"""
    platforms = {
        'video_platforms': [
            'YouTube (videos, shorts, playlists)',
            'TikTok',
            'Twitter/X',
            'Facebook',
            'Instagram (Reels, IGTV)',
            'Reddit',
            'Twitch',
            'Vimeo',
            'Dailymotion'
        ],
        'social_platforms': [
            'Instagram (Posts, Stories, Reels, IGTV)',
            'Twitter/X (Tweets, Threads)',
            'Facebook (Posts, Videos)',
            'Reddit (Posts, Images, Videos)',
            'LinkedIn (Posts)',
            'Pinterest (Pins)'
        ],
        'features': [
            'Auto-platform detection',
            'Bulk downloads',
            'Stories download',
            'Playlist support',
            'High quality downloads',
            'Metadata preservation',
            'Subtitle downloads'
        ]
    }
    return jsonify(platforms)

@app.route('/clear-downloads', methods=['POST'])
def clear_downloads():
    """Clear all downloaded files"""
    try:
        if os.path.exists(DOWNLOAD_DIR):
            shutil.rmtree(DOWNLOAD_DIR)
            os.makedirs(DOWNLOAD_DIR)
        return jsonify({'status': 'success', 'message': 'Downloads cleared successfully'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Error clearing downloads: {str(e)}'})


if __name__ == '__main__':
    print("=" * 60)
    print("UNIVERSAL SOCIAL MEDIA DOWNLOADER")
    print("=" * 60)
    print("Starting server...")
    print("Supported platforms: YouTube, Instagram, TikTok, Twitter/X, Facebook, Reddit, and more!")
    print("Features: Stories, Reels, Posts, Videos, Bulk downloads")
    print("")
    print("  Website (with downloader): http://localhost:5000")
    print("  Full App UI:               http://localhost:5000/app")
    print("=" * 60)
    app.run(debug=True, host='0.0.0.0', port=5000)
