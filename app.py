from flask import Flask, request, render_template, jsonify, send_file, send_from_directory
from flask_cors import CORS
import os
import tempfile
import threading
import requests
import json
import re
from datetime import datetime
import yt_dlp
import instaloader
from werkzeug.utils import secure_filename
import zipfile
import shutil

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here-change-this')

# Use /tmp on Render (no persistent disk on free tier), local dir otherwise
IS_RENDER = os.environ.get('RENDER', False)
if IS_RENDER:
    DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), 'anyvideo_downloads')
else:
    DOWNLOAD_DIR = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# Path to ffmpeg for merging video+audio
import shutil
FFMPEG_PATH = shutil.which('ffmpeg')
if not FFMPEG_PATH:
    FFMPEG_PATH = os.path.expanduser('~/AppData/Local/Microsoft/WinGet/Links')

class UniversalDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
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

    def validate_ydl_result(self, info, platform_name):
        """Normalize yt-dlp failures into a user-facing error."""
        if info is None:
            return {
                'status': 'error',
                'message': (
                    f'{platform_name} download failed. The server could not extract media details '
                    'from this URL. Please try another video or try again later.'
                )
            }
        return None

    def build_ydl_opts(self, path, template, format_selector='best', platform='generic', allow_partial_failures=False):
        """Create yt-dlp options that behave better on hosted environments."""
        ydl_opts = {
            'outtmpl': os.path.join(path, template),
            'format': format_selector,
            'merge_output_format': 'mp4',
            'retries': 3,
            'fragment_retries': 3,
            'extractor_retries': 3,
            'socket_timeout': 30,
            'noprogress': True,
            'quiet': True,
            'no_warnings': True,
            'geo_bypass': True,
            'http_headers': {
                'User-Agent': self.session.headers['User-Agent'],
                'Accept-Language': 'en-US,en;q=0.9',
            },
        }

        if allow_partial_failures:
            ydl_opts['ignoreerrors'] = True

        if FFMPEG_PATH:
            ydl_opts['ffmpeg_location'] = FFMPEG_PATH

        if platform == 'youtube':
            ydl_opts.update({
                'nocheckcertificate': True,
                'extractor_args': {
                    'youtube': {
                        'player_client': ['android', 'web', 'tv_embedded'],
                    }
                },
            })

        return ydl_opts
    
    def download_youtube_content(self, url, path):
        """Download YouTube videos, shorts, playlists"""
        try:
            ydl_opts = self.build_ydl_opts(
                path,
                '%(uploader)s - %(title)s.%(ext)s',
                format_selector='bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
                platform='youtube',
            )
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                error_result = self.validate_ydl_result(info, 'YouTube')
                if error_result:
                    return error_result

                if isinstance(info, dict) and 'entries' in info:  # Playlist
                    titles = [entry.get('title', 'Unknown') for entry in info['entries'] if entry]
                    return {
                        'status': 'success',
                        'message': f'Downloaded {len(titles)} videos from playlist',
                        'titles': titles[:5],  # Show first 5 titles
                        'type': 'playlist'
                    }
                else:  # Single video
                    return {
                        'status': 'success',
                        'message': 'YouTube content downloaded successfully!',
                        'title': info.get('title', 'Unknown'),
                        'uploader': info.get('uploader', 'Unknown'),
                        'type': 'video'
                    }
        except yt_dlp.utils.DownloadError as e:
            return {'status': 'error', 'message': f'YouTube error: {str(e)}'}
        except Exception as e:
            return {'status': 'error', 'message': f'YouTube error: {str(e)}'}
    
    def download_instagram_content(self, url, path):
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
            
            # Handle different Instagram URL types
            if '/stories/' in url:
                # Story URL
                username = self.extract_instagram_username(url)
                if username:
                    profile = instaloader.Profile.from_username(loader.context, username)
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
                    loader.download_post(post, target=username)
                    count += 1
                
                return {
                    'status': 'success',
                    'message': f'Downloaded {count} recent posts from {username}',
                    'type': 'profile'
                }
                
        except Exception as e:
            return {'status': 'error', 'message': f'Instagram error: {str(e)}'}
    
    def download_tiktok_content(self, url, path):
        """Download TikTok videos"""
        try:
            ydl_opts = self.build_ydl_opts(path, 'TikTok_%(uploader)s_%(title)s.%(ext)s')
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                error_result = self.validate_ydl_result(info, 'TikTok')
                if error_result:
                    return error_result
                return {
                    'status': 'success',
                    'message': 'TikTok video downloaded successfully!',
                    'title': info.get('title', 'TikTok Video'),
                    'uploader': info.get('uploader', 'Unknown'),
                    'type': 'video'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'TikTok error: {str(e)}'}
    
    def download_twitter_content(self, url, path):
        """Download Twitter/X videos, images, threads"""
        try:
            ydl_opts = self.build_ydl_opts(path, 'Twitter_%(uploader)s_%(title)s.%(ext)s')
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                error_result = self.validate_ydl_result(info, 'Twitter/X')
                if error_result:
                    return error_result
                return {
                    'status': 'success',
                    'message': 'Twitter content downloaded successfully!',
                    'title': info.get('title', 'Twitter Content'),
                    'uploader': info.get('uploader', 'Unknown'),
                    'type': 'tweet'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Twitter error: {str(e)}'}
    
    def download_facebook_content(self, url, path):
        """Download Facebook videos, posts"""
        try:
            ydl_opts = self.build_ydl_opts(path, 'Facebook_%(title)s.%(ext)s')
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                error_result = self.validate_ydl_result(info, 'Facebook')
                if error_result:
                    return error_result
                return {
                    'status': 'success',
                    'message': 'Facebook content downloaded successfully!',
                    'title': info.get('title', 'Facebook Content'),
                    'type': 'video'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Facebook error: {str(e)}'}
    
    def download_reddit_content(self, url, path):
        """Download Reddit videos, images, gifs"""
        try:
            ydl_opts = self.build_ydl_opts(path, 'Reddit_%(title)s.%(ext)s')
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                error_result = self.validate_ydl_result(info, 'Reddit')
                if error_result:
                    return error_result
                return {
                    'status': 'success',
                    'message': 'Reddit content downloaded successfully!',
                    'title': info.get('title', 'Reddit Post'),
                    'type': 'post'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Reddit error: {str(e)}'}
    
    def download_generic_content(self, url, path):
        """Download from any supported platform using yt-dlp"""
        try:
            ydl_opts = self.build_ydl_opts(path, '%(extractor)s_%(title)s.%(ext)s')
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                error_result = self.validate_ydl_result(info, 'This platform')
                if error_result:
                    return error_result
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
    
    def download_content(self, url, custom_path=None):
        """Main download function"""
        path = custom_path or DOWNLOAD_DIR
        platform = self.detect_platform(url)
        
        # Create timestamped folder for this download
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        download_folder = os.path.join(path, f"{platform}_{timestamp}")
        os.makedirs(download_folder, exist_ok=True)
        
        try:
            if platform == 'youtube':
                return self.download_youtube_content(url, download_folder)
            elif platform == 'instagram':
                return self.download_instagram_content(url, download_folder)
            elif platform == 'tiktok':
                return self.download_tiktok_content(url, download_folder)
            elif platform == 'twitter':
                return self.download_twitter_content(url, download_folder)
            elif platform == 'facebook':
                return self.download_facebook_content(url, download_folder)
            elif platform == 'reddit':
                return self.download_reddit_content(url, download_folder)
            else:
                # Try generic download for other platforms
                return self.download_generic_content(url, download_folder)
                
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

@app.route('/download', methods=['POST'])
def download():
    """Handle download requests"""
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        
        if not url:
            return jsonify({'status': 'error', 'message': 'URL is required'})
        
        # Detect platform automatically
        platform = downloader.detect_platform(url)
        
        # Start download
        result = downloader.download_content(url)
        result['platform'] = platform
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Server error: {str(e)}'})

@app.route('/bulk-download', methods=['POST'])
def bulk_download():
    """Handle bulk download requests"""
    try:
        data = request.get_json()
        urls = data.get('urls', [])
        
        if not urls:
            return jsonify({'status': 'error', 'message': 'URLs list is required'})
        
        results = []
        for url in urls:
            if url.strip():
                result = downloader.download_content(url.strip())
                result['url'] = url
                results.append(result)
        
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

@app.route('/health')
def health():
    """Health check for Render"""
    return jsonify({'status': 'ok'})

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
