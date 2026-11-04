"""MERRY AI DIGEST: manual, cloud-run Korean AI insight cards."""
import json
import os
import re
import textwrap
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zipfile import ZipFile, ZIP_DEFLATED

import requests
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont
from youtube_transcript_api import YouTubeTranscriptApi

OUT = Path('output_digest')
OUT.mkdir(exist_ok=True)
CHANNELS = {
    'Two Minute Papers': 'UCbfYPyITQ-7l4upoX8nvctg',
    'Fireship': 'UCsBjURrPoezykLs9EqgamOA',
    'Yannic Kilcher': 'UCZHmQk67mSJgfCCTn7xBfew',
}
TIMEOUT = 15
SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'}, 'subtitle': {'type': 'string'},
        'tag': {'type': 'string'},
        'cards': {'type': 'array', 'minItems': 5, 'maxItems': 5,
                  'items': {'type': 'object', 'properties': {
                      'head': {'type': 'string'}, 'desc': {'type': 'string'}},
                      'required': ['head', 'desc']}}
    },
    'required': ['title', 'subtitle', 'tag', 'cards']
}

def video_id(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if host in ('youtu.be', 'www.youtu.be'):
        candidate = parsed.path.strip('/').split('/')[0]
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        if parsed.path == '/watch':
            candidate = parse_qs(parsed.query).get('v', [''])[0]
        elif parsed.path.startswith(('/shorts/', '/live/', '/embed/')):
            candidate = parsed.path.split('/')[2]
        else:
            candidate = ''
    else:
        candidate = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate):
        raise ValueError('지원하지 않는 YouTube URL 형식입니다.')
    return candidate

def transcript(vid):
    api = YouTubeTranscriptApi()
    fetched = api.fetch(vid, languages=['ko', 'en'])
    result = ' '.join(item.text for item in fetched)
    if len(result.strip()) < 150:
        raise ValueError('자막이 너무 짧습니다.')
    return result[:28000]

def rss_candidates():
    ns = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
    candidates = []
    for name, cid in CHANNELS.items():
        try:
            resp = requests.get('https://www.youtube.com/feeds/videos.xml', params={'channel_id': cid}, timeout=TIMEOUT)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for entry in root.findall('a:entry', ns):
                vid = entry.findtext('yt:videoId', namespaces=ns)
                published = entry.findtext('a:published', namespaces=ns)
                title = entry.findtext('a:title', namespaces=ns)
                if vid and published:
                    candidates.append((published, vid, title, name))
        except (requests.RequestException, ET.ParseError) as exc:
            print(f'RSS skip {name}: {exc}')
    return sorted(candidates, reverse=True)

def github_sources():
    # Recently created + most stars is a proxy, NOT GitHub's official weekly trending ranking.
    since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    resp = requests.get('https://api.github.com/search/repositories',
        params={'q': f'topic:artificial-intelligence created:>={since}', 'sort': 'stars', 'order': 'desc', 'per_page': 10},
        headers={'Accept': 'application/vnd.github+json', 'Authorization': f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.getenv('GITHUB_TOKEN') else {'Accept': 'application/vnd.github+json'}, timeout=TIMEOUT)
    resp.raise_for_status()
    repos = resp.json().get('items', [])[:5]
    if not repos:
        raise ValueError('최근 7일 생성된 대상 저장소가 없습니다.')
    sources = [{'name': r['full_name'], 'url': r['html_url'], 'description': r.get('description') or '', 'stars': r['stargazers_count'], 'created_at': r['created_at']} for r in repos]
    return '최근 7일 생성된 AI 저장소를 현재 스타 수로 정렬한 참고 목록입니다. 공식 GitHub Trending 순위가 아닙니다.\n' + json.dumps(sources, ensure_ascii=False), sources

def get_source(mode, url):
    if mode == 'DIRECT_URL':
        vid = video_id(url)
        return '', [{'url': f'https://www.youtube.com/watch?v={vid}', 'video_id': vid}]
    if mode == 'LATEST_AI_YOUTUBE':
        for published, vid, title, name in rss_candidates():
            try:
                return f'채널: {name}\n제목: {title}\n게시일: {published}\n자막:\n{transcript(vid)}', [{'url': f'https://www.youtube.com/watch?v={vid}', 'channel': name, 'published': published, 'title': title}]
            except Exception as exc:
                print(f'Transcript unavailable {vid}: {exc}')
        raise ValueError('탐색한 영상에서 사용 가능한 자막을 확보하지 못했습니다.')
    if mode == 'GITHUB':
        return github_sources()
    raise ValueError('지원하지 않는 TARGET_MODE입니다.')

def font(size, bold=False):
    path = '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf' if bold else '/usr/share/fonts/truetype/nanum/NanumGothic.ttf'
    return ImageFont.truetype(path, size)

def wrapped(draw, text, fnt, width, max_lines):
    lines, line = [], ''
    for ch in str(text).replace('\r', ''):
        if ch == '\n':
            lines.append(line); line = ''; continue
        if draw.textbbox((0, 0), line + ch, font=fnt)[2] > width and line:
            lines.append(line); line = ch
        else:
            line += ch
    if line: lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textbbox((0, 0), lines[-1] + '…', font=fnt)[2] > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += '…'
    return lines

def draw_block(draw, text, xy, fnt, fill, width, max_lines, spacing=15):
    for i, line in enumerate(wrapped(draw, text, fnt, width, max_lines)):
        draw.text((xy[0], xy[1] + i * (fnt.size + spacing)), line, font=fnt, fill=fill)

def render(data, sources):
    cover = Image.new('RGB', (1080, 1080), '#12161c')
    d = ImageDraw.Draw(cover)
    d.text((75, 110), 'MERRY AI HUB  |  ' + data['tag'][:25], font=font(27, True), fill='#00d084')
    draw_block(d, data['title'], (75, 365), font(65, True), 'white', 925, 4, 25)
    draw_block(d, data['subtitle'], (75, 760), font(30), '#b5bfcb', 925, 3)
    d.text((75, 978), 'MERRY AI DIGEST  ·  출처는 sources.json 참조', font=font(23), fill='#9ba7b3')
    cover.save(OUT / '00_cover.png')
    for i, card in enumerate(data['cards'], 1):
        img = Image.new('RGB', (1080, 1080), '#f8f9fa')
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((75, 85, 265, 150), radius=12, fill='#12161c')
        d.text((96, 101), f'POINT {i:02d}', font=font(27, True), fill='white')
        draw_block(d, card['head'], (75, 230), font(47, True), '#151b22', 930, 3, 20)
        d.line((75, 445, 1005, 445), fill='#d9dfe4', width=3)
        draw_block(d, card['desc'], (75, 505), font(35), '#34404d', 930, 10, 18)
        d.text((75, 975), 'MERRY AI DIGEST  |  원문 출처: sources.json', font=font(24), fill='#737e89')
        img.save(OUT / f'card_{i:02d}.png')

def main():
    if not os.getenv('GEMINI_API_KEY'):
        raise ValueError('GEMINI_API_KEY secret이 없습니다.')
    mode = os.getenv('TARGET_MODE', 'DIRECT_URL')
    source, sources = get_source(mode, os.getenv('YOUTUBE_URL', ''))
    (OUT / 'sources.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding='utf-8')
    prompt = ('아래 원문에만 근거하여 한국어로 실무용 카드뉴스를 작성하세요. 추측을 사실처럼 말하지 마세요. '
              '광고/제휴가 확인되지 않았다면 광고 문구를 임의로 만들지 마세요. '
              '본문 카드 정확히 5개, 각 카드 제목 30자 이내, 설명 150자 이내. '
              '출처가 불확실한 가격이나 기능은 확정하지 마세요.\n\n' + source)
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    contents = [prompt, types.Part.from_uri(file_uri=sources[0]['url'], mime_type='video/mp4')] if mode == 'DIRECT_URL' else prompt
    response = client.models.generate_content(model='gemini-3.5-flash-lite', contents=contents,
        config=types.GenerateContentConfig(response_mime_type='application/json', response_json_schema=SCHEMA))
    data = json.loads(response.text)
    if len(data.get('cards', [])) != 5:
        raise ValueError('Gemini 응답에 본문 카드 5개가 없습니다.')
    (OUT / 'digest.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    render(data, sources)
    with ZipFile(OUT / 'merry_ai_digest.zip', 'w', ZIP_DEFLATED) as z:
        for p in sorted(OUT.glob('*.png')):
            z.write(p, p.name)
        for p in (OUT / 'sources.json', OUT / 'digest.json'):
            z.write(p, p.name)
    print('Created 6 cards and ZIP.')

if __name__ == '__main__':
    main()

