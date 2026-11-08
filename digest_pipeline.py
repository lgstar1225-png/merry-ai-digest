"""MERRY AI DIGEST: manual, cloud-run Korean AI insight cards."""
import json
import os
import re
import textwrap
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zipfile import ZipFile, ZIP_DEFLATED

import requests
import httpx
from bs4 import BeautifulSoup
from google import genai
from google.genai import errors, types
from PIL import Image, ImageDraw, ImageFont
from youtube_transcript_api import YouTubeTranscriptApi

OUT = Path('output_digest')
OUT.mkdir(exist_ok=True)
CHANNELS = {item['name']: item['id'] for item in json.loads(Path('channels.json').read_text(encoding='utf-8'))}
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
            raise RuntimeError(f'채널 RSS 조회 실패: {name}') from exc
    return sorted(candidates, reverse=True)

def github_sources():
    url = 'https://github.com/trending?since=weekly'
    resp = requests.get(url, headers={'User-Agent': 'MERRY-AI-DIGEST'}, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    sources = []
    for article in soup.select('article.Box-row'):
        link = article.select_one('h2 a')
        if not link or not link.get('href'):
            continue
        name = link['href'].strip('/')
        if name.count('/') != 1:
            continue
        description = article.select_one('p')
        sources.append({
            'name': name,
            'url': 'https://github.com/' + name,
            'description': description.get_text(' ', strip=True) if description else '',
            'weekly_trending_position': len(sources) + 1,
        })
    if not sources:
        raise ValueError('GitHub 주간 인기 목록을 읽지 못했습니다.')
    instructions = (
        '다음은 GitHub 공식 주간 인기 전체 목록입니다. '
        'AI나 자동화와 관련 있는 도구를 우선 골라 우리 업무에 쓸 수 있는지 쉽게 설명하세요. '
        '설명에서 확인되지 않는 기능은 추측하지 마세요. '
        '이 목록은 전체 주간 인기 목록이며 AI 도구만의 순위가 아닙니다.\n'
    )
    return instructions + json.dumps(sources, ensure_ascii=False), sources

def get_source(mode, url):
    if mode == 'DIRECT_URL':
        vid = video_id(url)
        return '', [{'url': f'https://www.youtube.com/watch?v={vid}', 'video_id': vid}]
    if mode == 'LATEST_AI_YOUTUBE':
        candidates = rss_candidates()
        if not candidates:
            raise ValueError('채널의 최신 영상을 가져오지 못했습니다.')
        published, vid, title, name = candidates[0]
        return f'채널: {name}\n제목: {title}\n게시일: {published}', [{'url': f'https://www.youtube.com/watch?v={vid}', 'video_id': vid, 'channel': name, 'published': published, 'title': title}]
    if mode == 'GITHUB':
        return github_sources()
    raise ValueError('지원하지 않는 TARGET_MODE입니다.')

def font(size, bold=False):
    path = '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf' if bold else '/usr/share/fonts/truetype/nanum/NanumGothic.ttf'
    return ImageFont.truetype(path, size)

def wrapped(draw, text, fnt, width, max_lines):
    lines = []
    for paragraph in str(text).replace('\r', '').split('\n'):
        line = ''
        for word in paragraph.split():
            candidate = f'{line} {word}' if line else word
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ''
            for ch in word:
                if line and draw.textbbox((0, 0), line + ch, font=fnt)[2] > width:
                    lines.append(line)
                    line = ''
                line += ch
        lines.append(line)
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

def generate_with_retry(client, contents):
    """일시적인 오류는 다시 시도하고, 응답 형식도 확인한다."""
    config = types.GenerateContentConfig(response_mime_type='application/json', response_json_schema=SCHEMA)

    def request():
        for attempt in range(4):
            try:
                return client.models.generate_content(model='gemini-3.5-flash-lite', contents=contents, config=config)
            except (httpx.TransportError, TimeoutError, errors.ServerError, errors.ClientError) as exc:
                code = getattr(exc, 'code', None)
                if isinstance(exc, errors.ClientError) and code != 429:
                    print(f'Gemini 요청 실패: 요청 오류 {code}입니다. 다시 시도하지 않습니다.', flush=True)
                    raise
                if attempt == 3:
                    fallback = os.getenv('GEMINI_FALLBACK_MODEL', '').strip()
                    if code in (429, 503) and fallback:
                        print('기본 모델 4번째 시도 실패: 0초 후 예비 모델로 1번 시도합니다.', flush=True)
                        try:
                            return client.models.generate_content(model=fallback, contents=contents, config=config)
                        except Exception as fallback_exc:
                            print(f'예비 모델 요청 최종 실패: {type(fallback_exc).__name__}, 오류 코드 {getattr(fallback_exc, "code", "없음")}', flush=True)
                            raise
                    print(f'Gemini 요청 최종 실패: {type(exc).__name__}, 오류 코드 {code if code is not None else "없음"}', flush=True)
                    raise
                delay = (5, 15, 45)[attempt]
                print(f'Gemini {attempt + 1}번째 시도 실패: {delay}초 기다린 뒤 {attempt + 2}번째 시도합니다.', flush=True)
                time.sleep(delay)

    for attempt in range(2):
        response = request()
        try:
            data = json.loads(response.text)
            if not isinstance(data, dict) or not isinstance(data.get('cards'), list) or len(data['cards']) != 5:
                raise ValueError('카드가 5개가 아닙니다.')
            return data
        except (ValueError, TypeError) as exc:
            if attempt == 1:
                print('Gemini 응답 최종 실패: JSON이 잘못됐거나 카드가 5개가 아닙니다.', flush=True)
                raise ValueError('Gemini 응답이 잘못됐거나 카드가 5개가 아닙니다.') from exc
            print('Gemini 응답이 잘못됐거나 카드가 5개가 아닙니다. 0초 후 2번째 요청을 합니다.', flush=True)

def main():
    if not os.getenv('GEMINI_API_KEY'):
        raise ValueError('GEMINI_API_KEY secret이 없습니다.')
    mode = os.getenv('TARGET_MODE', 'DIRECT_URL')
    source, sources = get_source(mode, os.getenv('YOUTUBE_URL', ''))
    (OUT / 'sources.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding='utf-8')
    prompt = ('첨부된 영상 또는 아래 자료에 근거해 한국어 카드뉴스를 작성하세요. '
              '10살 어린이와 60대 초보자도 이해할 수 있도록 쉬운 말과 짧은 문장을 사용하세요. '
              '어려운 용어가 꼭 필요하면 바로 옆에 쉬운 뜻을 설명하세요. '
              '각 카드에 한 가지 핵심만 담고, 필요하면 생활 속 쉬운 예를 들어주세요. '
              '자료의 주장과 확인된 사실을 구별하고, 알 수 없는 내용은 추측하지 마세요. '
              '제목은 30자 이내, 각 본문은 100자 이내로 작성하세요. '
              '본문 카드 5개는 각각 무엇인지, 왜 관심을 받는지, 어떻게 쓰는지, '
              '알아둘 점, 한 줄 정리를 설명하세요. '
              '자료가 부족하면 부족하다고 명시하고 확인되지 않은 광고 문구를 넣지 마세요.\n\n' + source)
    if mode == 'GITHUB':
        prompt += (
            '\nGitHub 모드에서는 AI 또는 자동화와 관련된 서로 다른 도구를 최대 5개 고르세요. '
            '각 카드 제목에 도구의 실제 저장소 이름을 적고 한 카드에 한 도구씩 설명하세요. '
            '확실한 도구가 5개 미만이면 나머지 카드는 관련 도구를 확인하지 못했다고 쓰세요. '
            '확인하지 못한 도구의 기능이나 업무 활용법은 만들어내지 마세요. '
            '전체 인기 목록에서 고른 도구임을 밝히고 AI 분야 공식 순위라고 부르지 마세요.'
        )
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'], http_options=types.HttpOptions(timeout=300000))
    contents = [prompt, types.Part.from_uri(file_uri=sources[0]['url'], mime_type='video/mp4')] if mode in ('DIRECT_URL', 'LATEST_AI_YOUTUBE') else prompt
    data = generate_with_retry(client, contents)
    (OUT / 'digest.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    report_lines = [data['title'], data['subtitle'], '']
    for index, card in enumerate(data['cards'], 1):
        report_lines.extend([f"{index}. {card['head']}", card['desc'], ''])
    report_lines.append('출처')
    report_lines.extend(source['url'] for source in sources)
    (OUT / 'report.txt').write_text('\n'.join(report_lines) + '\n', encoding='utf-8')
    render(data, sources)
    with ZipFile(OUT / 'merry_ai_digest.zip', 'w', ZIP_DEFLATED) as z:
        for p in sorted(OUT.glob('*.png')):
            z.write(p, p.name)
        for p in (OUT / 'sources.json', OUT / 'digest.json', OUT / 'report.txt'):
            z.write(p, p.name)
    if mode in ('DIRECT_URL', 'LATEST_AI_YOUTUBE'):
        summary_dir = Path('summaries')
        summary_dir.mkdir(exist_ok=True)
        summary_file = summary_dir / (sources[0]['video_id'] + '.json')
        summary_file.write_text(json.dumps({'source': sources[0], 'summary': data}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Created 6 cards and ZIP.')

if __name__ == '__main__':
    main()

