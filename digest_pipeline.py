"""MERRY AI DIGEST: manual, cloud-run Korean AI insight cards."""
import copy
import html
import json
import os
import re
import smtplib
import ssl
import textwrap
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
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

def render(data, sources, mode):
    if mode in ('DIRECT_URL', 'LATEST_AI_YOUTUBE'):
        img = Image.new('RGB', (1080, 1080), '#f8f9fa')
        d = ImageDraw.Draw(img)
        d.text((75, 80), 'MERRY AI DIGEST  |  영상 핵심 한 장', font=font(27, True), fill='#00865b')
        draw_block(d, data['title'], (75, 180), font(48, True), '#151b22', 930, 3, 15)
        draw_block(d, data['cards'][0]['desc'], (75, 420), font(34), '#34404d', 930, 10, 16)
        d.text((75, 975), '영상 출처: sources.json  |  내용 확인 필요', font=font(23), fill='#737e89')
        img.save(OUT / '00_cover.png')
        return

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
        draw_block(d, clean_head(card['head']), (75, 230), font(47, True), '#151b22', 930, 3, 20)
        d.line((75, 445, 1005, 445), fill='#d9dfe4', width=3)
        draw_block(d, card['desc'], (75, 505), font(35), '#34404d', 930, 10, 18)
        d.text((75, 975), 'MERRY AI DIGEST  |  원문 출처: sources.json', font=font(24), fill='#737e89')
        img.save(OUT / f'card_{i:02d}.png')

def clean_head(head):
    return re.sub(r'^\s*\d+[.)]\s+', '', head)

def generate_with_retry(client, contents, schema, expected_cards, media_resolution=None, delays=(5, 15, 45)):
    """일시적인 오류는 다시 시도하고, 응답 형식도 확인한다."""
    extra = {'media_resolution': media_resolution} if media_resolution else {}
    config = types.GenerateContentConfig(response_mime_type='application/json', response_json_schema=schema, **extra)

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
                delay = delays[attempt]
                print(f'Gemini {attempt + 1}번째 시도 실패: {delay}초 기다린 뒤 {attempt + 2}번째 시도합니다.', flush=True)
                time.sleep(delay)

    for attempt in range(2):
        response = request()
        try:
            data = json.loads(response.text)
            if not isinstance(data, dict) or not isinstance(data.get('cards'), list) or len(data['cards']) != expected_cards:
                raise ValueError(f'카드가 {expected_cards}개가 아닙니다.')
            return data
        except (ValueError, TypeError) as exc:
            if attempt == 1:
                print(f'Gemini 응답 최종 실패: JSON이 잘못됐거나 카드가 {expected_cards}개가 아닙니다.', flush=True)
                raise ValueError(f'Gemini 응답이 잘못됐거나 카드가 {expected_cards}개가 아닙니다.') from exc
            print(f'Gemini 응답이 잘못됐거나 카드가 {expected_cards}개가 아닙니다. 0초 후 2번째 요청을 합니다.', flush=True)

# ---------- 주간 모드 (WEEKLY_YOUTUBE): 10곳 최근 7일 → 쇼츠 제외 → 조회수 상위 N편 → 한 장 + 100점 → 메일 ----------
KST = timezone(timedelta(hours=9))
FEED_NS = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015',
           'media': 'http://search.yahoo.com/mrss/'}
SCORE_MAX = {'score_fit': 25, 'score_change': 30, 'score_evidence': 20, 'score_easy': 15, 'score_keep': 10}
SCORE_CAP = 69  # 100점표 규칙: 우리가 써 보기 전에는 70점 미만
WEEKLY_RETRY_DELAYS = (30, 60, 90)  # 429(분당 사용량 한도)는 1분 넘게 기다려야 풀린다
WEEKLY_SECOND_PASS_WAIT = 120
WEEKLY_SCHEMA = copy.deepcopy(SCHEMA)
WEEKLY_SCHEMA['properties']['cards']['minItems'] = 1
WEEKLY_SCHEMA['properties']['cards']['maxItems'] = 1
WEEKLY_SCHEMA['properties'].update({k: {'type': 'integer', 'minimum': 0, 'maximum': v} for k, v in SCORE_MAX.items()})
WEEKLY_SCHEMA['properties'].update({
    'confidence': {'type': 'string', 'enum': ['높음', '보통', '낮음']},
    'reason': {'type': 'string'}, 'try_next': {'type': 'string'}, 'ch6_idea': {'type': 'string'},
})
WEEKLY_SCHEMA['required'] = SCHEMA['required'] + list(SCORE_MAX) + ['confidence', 'reason', 'try_next', 'ch6_idea']


def env_number(name, default):
    try:
        return int(os.getenv(name, '') or default)
    except ValueError:
        return default


def weekly_candidates(now=None, days=7):
    """채널 RSS에서 최근 영상(쇼츠 제외)을 조회수 순으로 고른다. 실패한 채널은 건너뛴다."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    videos, failed = [], []
    for name, cid in CHANNELS.items():
        try:
            resp = requests.get('https://www.youtube.com/feeds/videos.xml', params={'channel_id': cid}, timeout=TIMEOUT)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except (requests.RequestException, ET.ParseError) as exc:
            print(f'채널 RSS 조회 실패, 건너뜀: {name} ({type(exc).__name__})', flush=True)
            failed.append(name)
            continue
        for entry in root.findall('a:entry', FEED_NS):
            vid = entry.findtext('yt:videoId', namespaces=FEED_NS)
            published = entry.findtext('a:published', namespaces=FEED_NS)
            link = entry.find('a:link', FEED_NS)
            href = link.get('href', '') if link is not None else ''
            if not vid or not published or '/shorts/' in href:
                continue
            try:
                pub = datetime.fromisoformat(published.replace('Z', '+00:00'))
            except ValueError:
                continue
            if pub < since:
                continue
            stats = entry.find('media:group/media:community/media:statistics', FEED_NS)
            try:
                views = int(stats.get('views', 0)) if stats is not None else 0
            except ValueError:
                views = 0
            videos.append({'url': f'https://www.youtube.com/watch?v={vid}', 'video_id': vid, 'channel': name,
                           'title': entry.findtext('a:title', namespaces=FEED_NS) or '', 'published': published,
                           'views': views})
    videos.sort(key=lambda v: v['views'], reverse=True)
    per_channel = max(1, env_number('WEEKLY_PER_CHANNEL', 1))  # 한 채널이 독차지하지 않게
    picked, counts = [], {}
    for v in videos:
        if counts.get(v['channel'], 0) < per_channel:
            picked.append(v)
            counts[v['channel']] = counts.get(v['channel'], 0) + 1
    return picked[:max(1, env_number('WEEKLY_COUNT', 5))], failed


def weekly_prompt(video):
    return (
        '첨부된 유튜브 영상을 보고 한국어로 "핵심 한 장"을 만드세요.\n'
        f'채널: {video["channel"]}\n제목: {video["title"]}\n\n'
        '10살 어린이와 60대 초보자도 이해할 수 있게 쉬운 말과 짧은 문장을 쓰세요. 어려운 용어는 바로 옆에 쉬운 뜻을 붙이세요.\n'
        'title은 30자 이내 한국어 제목, subtitle은 한 줄 설명, tag는 짧은 분야 이름입니다.\n'
        'cards는 1개입니다. head는 핵심 한 줄, desc는 220자 이내로 영상에서 확인된 구체적 사례·수치, '
        '우리 일에 바로 쓸 수 있는지, 확인이 필요한 한계를 담으세요.\n'
        '우리 일 = 동대문 의류 부자재 도소매 1인 사업자의 블로그·쇼츠·롱폼 유튜브·홈페이지·AI 자동화.\n'
        '점수: score_fit 우리 일과 맞나(0~25), score_change 실제로 얼마나 바뀌나(0~30), '
        'score_evidence 작동이 실제로 확인됐나(0~20), score_easy 시작이 쉬운가(0~15), score_keep 계속 쓰기 좋은가(0~10).\n'
        '우리는 아직 써 보지 않았으므로 합계가 70점 미만이 되게 주고 confidence는 보통 또는 낮음으로 하세요. '
        '확인 안 된 효과는 점수에 넣지 말고, 하나마나한 이야기면 낮게 주세요.\n'
        'reason은 점수 이유 한 줄(60자 이내), try_next는 해볼 만한 가장 작은 시험 한 가지(60자 이내)입니다.\n'
        'ch6_idea는 「AI한테 시켜봤습니다」 쇼츠 채널(작은 가게 사장이 AI에게 실제로 일을 맡겨 보는 기록)의 소재 한 줄(80자 이내)입니다. '
        '"사장님이 이걸 AI한테 시켜 보면?" 형태로 쓰고, 영상에 나온 AI 기능으로 가게 일(주문 정리, 상품 소개 글, 손님 답장, 사진, 재고, 블로그·쇼츠)을 '
        '실제로 맡겨 볼 수 있을 때만 쓰세요. 억지로 연결하지 말고, 연구·이론·업계 소식처럼 직접 맡길 일이 없으면 "해당 없음"이라고 쓰세요.\n'
        '가격·수치·후기를 지어내지 말고, 영상에서 확인하지 못한 사실은 만들지 마세요. '
        '금액은 영상에 나온 단위 그대로 쓰고, 원화 등 다른 통화로 바꾸거나 직접 계산하지 마세요.'
    )


def score_total(data):
    """점수 합계는 코드가 계산한다. 항목별 상한을 지키고, 써 보기 전이라 69점을 넘기지 않는다."""
    total = 0
    for key, top in SCORE_MAX.items():
        try:
            value = int(data.get(key, 0))
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(top, value))
        data[key] = value
        total += value
    data['score_raw'] = total
    data['score_total'] = min(total, SCORE_CAP)
    if data.get('confidence') not in ('보통', '낮음'):
        data['confidence'] = '보통'
    return data['score_total']


def render_weekly(data, video, path):
    img = Image.new('RGB', (1080, 1080), '#f8f9fa')
    d = ImageDraw.Draw(img)
    d.text((75, 80), 'MERRY AI DIGEST  |  이번 주 영상 핵심 한 장', font=font(27, True), fill='#00865b')
    draw_block(d, data['title'], (75, 180), font(48, True), '#151b22', 930, 3, 15)
    draw_block(d, data['cards'][0]['desc'], (75, 420), font(34), '#34404d', 930, 9, 16)
    footer = f"{video['channel']}  ·  조회수 {video['views']:,}회  ·  {data['score_total']}/100점 (확신 {data['confidence']})"
    draw_block(d, footer, (75, 905), font(27, True), '#151b22', 930, 1, 10)
    d.text((75, 975), 'AI 요약  |  사실관계는 원본 영상으로 확인 필요', font=font(23), fill='#737e89')
    img.save(path)


def week_label(now_kst):
    return f'{now_kst.month}월 {(now_kst.day - 1) // 7 + 1}주차'


def ics_escape(text):
    return str(text).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def ics_fold(line):
    """iCalendar 규칙: 한 줄 75바이트를 넘으면 접는다 (한글이 깨지지 않게 글자 단위)."""
    parts, cur = [], b''
    for ch in line:
        b = ch.encode('utf-8')
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode('utf-8'))
            cur = b''
        cur += b
    parts.append(cur.decode('utf-8'))
    return '\r\n '.join(parts)


def event_times(now_kst, hour):
    day = now_kst.strftime('%Y%m%d')
    return f'{day}T{hour:02d}0000', f'{day}T{hour:02d}1500'


def video_list_text(results):
    return '\n'.join(f"{i}. {r['video']['title']} {r['video']['url']}" for i, r in enumerate(results, 1))


def build_ics(results, now_kst, hour):
    start, end = event_times(now_kst, hour)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//MERRY//AI DIGEST//KO', 'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
        'BEGIN:VTIMEZONE', 'TZID:Asia/Seoul', 'BEGIN:STANDARD', 'DTSTART:19700101T000000',
        'TZOFFSETFROM:+0900', 'TZOFFSETTO:+0900', 'TZNAME:KST', 'END:STANDARD', 'END:VTIMEZONE',
        'BEGIN:VEVENT', f'UID:merry-ai-digest-{start}@merry-ai-digest', f'DTSTAMP:{stamp}',
        f'DTSTART;TZID=Asia/Seoul:{start}', f'DTEND;TZID=Asia/Seoul:{end}',
        'SUMMARY:' + ics_escape('AI 다이제스트 읽기'),
        'DESCRIPTION:' + ics_escape(video_list_text(results)),
        'END:VEVENT', 'END:VCALENDAR',
    ]
    return '\r\n'.join(ics_fold(line) for line in lines) + '\r\n'


def calendar_link(results, now_kst, hour):
    start, end = event_times(now_kst, hour)
    return ('https://calendar.google.com/calendar/render?action=TEMPLATE&text=' + quote('AI 다이제스트 읽기')
            + f'&dates={start}/{end}&ctz=Asia/Seoul&details=' + quote(video_list_text(results)[:1500]))


def ch6_ideas(results):
    ideas = []
    for r in results:
        idea = (r['data'] or {}).get('ch6_idea', '').strip()
        if idea and '해당 없음' not in idea:
            ideas.append((idea, r['video']))
    return ideas


def weekly_markdown(results, failed, now_kst):
    lines = [f'# MERRY AI 다이제스트 {now_kst:%Y-%m-%d} ({week_label(now_kst)})', '',
             'AI 요약이라 사실관계는 원본 영상으로 확인 필요. 점수는 우리가 써 보기 전이라 69점이 상한.', '',
             '## 6번 채널(AI한테 시켜봤습니다) 소재 후보', '']
    ideas = ch6_ideas(results)
    lines += [f"- {idea} — 출처: [{v['title']}]({v['url']}) ({v['channel']})" for idea, v in ideas] or ['- 이번 주는 맞는 소재 없음']
    lines += ['', '## 영상별 요약 (점수 높은 순)', '']
    for i, r in enumerate(results, 1):
        v, d = r['video'], r['data']
        lines.append(f"### {i}. [{v['title']}]({v['url']})")
        lines.append(f"- {v['channel']} · 조회수 {v['views']:,}회 · 게시 {v['published'][:10]}")
        if not d:
            lines += ['- 요약 실패 (다음 주에 다시 시도하거나 직접 확인)', '']
            continue
        detail = ' / '.join(f'{k.replace("score_", "")} {d[k]}' for k in SCORE_MAX)
        lines += [f"- 점수 {d['score_total']}/100 (확신 {d['confidence']}) — {detail}",
                  f"- 핵심: {d['cards'][0]['head']}", f"- {d['cards'][0]['desc']}",
                  f"- 이유: {d.get('reason', '')}", f"- 해볼 만한 것: {d.get('try_next', '')}", '']
    if failed:
        lines.append('읽지 못한 채널: ' + ', '.join(failed))
    return '\n'.join(lines) + '\n'


def build_email(results, failed, now_kst, sender, hour):
    ok = [r for r in results if r['data']]
    subject = f'[MERRY AI 다이제스트] {week_label(now_kst)} · {len(results)}편'
    if ok:
        best = ok[0]['data']
        headline = f"가장 쓸 만한 영상: {best['title']} ({best['score_total']}점)"
    else:
        headline = '이번 주는 요약에 성공한 영상이 없습니다.'
    ideas = ch6_ideas(results)
    cal = calendar_link(results, now_kst, hour)
    e = html.escape
    plain = [subject, '', '이번 주 한 줄: ' + headline, '', '[6번 채널 소재 후보]']
    plain += [f'- {idea} ({v["title"]})' for idea, v in ideas] or ['- 이번 주는 맞는 소재 없음']
    plain.append('')
    parts = [f'<div style="font-family:sans-serif;max-width:640px;margin:auto;color:#151b22">',
             f'<h2 style="margin:0 0 6px">{e(subject)}</h2>',
             f'<p style="font-size:17px;background:#eef7f2;padding:12px;border-radius:8px"><b>이번 주 한 줄</b><br>{e(headline)}</p>',
             f'<p><a href="{e(cal)}">구글 캘린더에 &quot;AI 다이제스트 읽기&quot; 일정 넣기</a> (첨부한 .ics 파일로도 넣을 수 있습니다)</p>',
             '<h3>6번 채널(AI한테 시켜봤습니다) 소재 후보</h3><ul>']
    parts += [f'<li>{e(idea)} — <a href="{e(v["url"])}">{e(v["title"])}</a></li>' for idea, v in ideas] or ['<li>이번 주는 맞는 소재 없음</li>']
    parts.append('</ul>')
    images = []
    for i, r in enumerate(results, 1):
        v, d = r['video'], r['data']
        meta = f"{v['channel']} · 조회수 {v['views']:,}회"
        parts.append(f'<hr><h3 style="margin-bottom:4px">{i}. <a href="{e(v["url"])}">{e(v["title"])}</a></h3><p style="color:#5b6570;margin-top:0">{e(meta)}</p>')
        plain += [f"{i}. {v['title']}", f'   {meta}', f"   {v['url']}"]
        if not d:
            parts.append('<p>요약 실패 — 원본 영상을 직접 확인해 주세요.</p>')
            plain += ['   요약 실패', '']
            continue
        score = f"{d['score_total']}/100점 (확신 {d['confidence']})"
        parts.append(f'<p><b>{e(score)}</b> · {e(d["title"])}</p><p>{e(d["cards"][0]["desc"])}</p>'
                     f'<p>이유: {e(d.get("reason", ""))}<br>해볼 만한 것: {e(d.get("try_next", ""))}</p>')
        if r.get('image'):
            cid = make_msgid(domain='merry-ai-digest')
            images.append((cid, r['image']))
            parts.append(f'<img src="cid:{cid[1:-1]}" alt="핵심 한 장" style="width:100%;max-width:540px;border-radius:8px">')
        plain += [f'   {score}', f"   {d['cards'][0]['desc']}", f"   이유: {d.get('reason', '')}",
                  f"   해볼 만한 것: {d.get('try_next', '')}", '']
    if failed:
        parts.append(f'<p style="color:#5b6570">읽지 못한 채널: {e(", ".join(failed))}</p>')
        plain.append('읽지 못한 채널: ' + ', '.join(failed))
    note = 'AI 요약이라 사실관계는 원본 영상으로 확인 필요합니다. 점수는 우리가 써 보기 전이라 69점이 상한입니다.'
    parts.append(f'<hr><p style="color:#737e89;font-size:13px">{note}</p></div>')
    plain += ['', '구글 캘린더에 일정 넣기: ' + cal, note]

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = sender
    msg.set_content('\n'.join(plain))
    msg.add_alternative(''.join(parts), subtype='html')
    html_part = msg.get_payload()[1]
    for cid, path in images:
        html_part.add_related(Path(path).read_bytes(), 'image', 'png', cid=cid)
    msg.add_attachment(build_ics(results, now_kst, hour).encode('utf-8'), maintype='text', subtype='calendar',
                       filename='ai-digest.ics')
    return msg


def send_email(msg, sender, password):
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ssl.create_default_context(), timeout=60) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def run_weekly(client, now=None):
    now = now or datetime.now(timezone.utc)
    now_kst = now.astimezone(KST)
    videos, failed = weekly_candidates(now)
    if not videos:
        raise ValueError('최근 7일 영상(쇼츠 제외)을 찾지 못했습니다.')
    print(f'고른 영상 {len(videos)}편, 읽지 못한 채널 {len(failed)}곳', flush=True)
    (OUT / 'sources.json').write_text(json.dumps(videos, ensure_ascii=False, indent=2), encoding='utf-8')
    pause = env_number('WEEKLY_PAUSE', 45)

    def summarize(i, video):
        print(f'[{i}/{len(videos)}] {video["channel"]} 요약 시작', flush=True)
        try:
            contents = [weekly_prompt(video), types.Part.from_uri(file_uri=video['url'], mime_type='video/mp4')]
            data = generate_with_retry(client, contents, WEEKLY_SCHEMA, 1,
                                       media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                                       delays=WEEKLY_RETRY_DELAYS)
            for card in data['cards']:
                card['head'] = clean_head(card['head'])
            score_total(data)
            return data
        except Exception as exc:  # 한 편이 실패해도 나머지는 계속
            print(f'[{i}/{len(videos)}] 요약 실패: {type(exc).__name__}', flush=True)
            return None

    results = []
    for i, video in enumerate(videos, 1):
        if i > 1 and pause > 0:
            time.sleep(pause)  # 분당 사용량 한도를 피하려고 영상 사이에 쉰다
        results.append({'video': video, 'data': summarize(i, video)})
    # 실패한 영상은 한도가 풀리도록 2분 쉰 뒤 한 번 더 시도한다
    for i, r in enumerate(results, 1):
        if r['data'] is None:
            print(f'[{i}/{len(videos)}] 실패한 영상 다시 시도: {WEEKLY_SECOND_PASS_WAIT}초 쉼', flush=True)
            time.sleep(WEEKLY_SECOND_PASS_WAIT)
            r['data'] = summarize(i, r['video'])
    ok = sorted((r for r in results if r['data']), key=lambda r: r['data']['score_total'], reverse=True)
    results = ok + [r for r in results if not r['data']]
    for i, r in enumerate(results, 1):
        if r['data']:
            r['image'] = OUT / f'weekly_{i:02d}.png'
            render_weekly(r['data'], r['video'], r['image'])
    (OUT / 'digest.json').write_text(json.dumps([{'video': r['video'], 'data': r['data']} for r in results],
                                                ensure_ascii=False, indent=2), encoding='utf-8')
    markdown = weekly_markdown(results, failed, now_kst)
    (OUT / 'weekly.md').write_text(markdown, encoding='utf-8')
    archive = Path('weekly')
    archive.mkdir(exist_ok=True)
    (archive / f'{now_kst:%Y-%m-%d}.md').write_text(markdown, encoding='utf-8')
    (archive / 'latest.md').write_text(markdown, encoding='utf-8')
    hour = min(23, max(0, env_number('WEEKLY_EVENT_HOUR', 22)))
    (OUT / 'ai-digest.ics').write_text(build_ics(results, now_kst, hour), encoding='utf-8', newline='')
    with ZipFile(OUT / 'merry_ai_digest.zip', 'w', ZIP_DEFLATED) as z:
        for p in sorted(OUT.glob('weekly_*.png')):
            z.write(p, p.name)
        for name in ('sources.json', 'digest.json', 'weekly.md', 'ai-digest.ics'):
            z.write(OUT / name, name)
    if not ok:
        raise RuntimeError('이번 주 영상 요약이 모두 실패했습니다.')
    sender, password = os.getenv('GMAIL_ADDRESS', '').strip(), os.getenv('GMAIL_APP_PASSWORD', '').replace(' ', '').strip()
    if not sender or not password:
        print('메일 설정 없음: 메일은 건너뜁니다 (결과는 ZIP과 weekly/ 폴더에 있음).', flush=True)
    else:
        send_email(build_email(results, failed, now_kst, sender, hour), sender, password)
        print('메일 보냄.', flush=True)
    print(f'주간 다이제스트 완료: 성공 {len(ok)}편 / 전체 {len(results)}편', flush=True)
    return results


def main():
    if not os.getenv('GEMINI_API_KEY'):
        raise ValueError('GEMINI_API_KEY secret이 없습니다.')
    mode = os.getenv('TARGET_MODE', 'DIRECT_URL') or 'DIRECT_URL'
    if mode == 'WEEKLY_YOUTUBE':
        client = genai.Client(api_key=os.environ['GEMINI_API_KEY'], http_options=types.HttpOptions(timeout=300000))
        run_weekly(client)
        return
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
    if mode in ('DIRECT_URL', 'LATEST_AI_YOUTUBE'):
        prompt = prompt.replace('각 본문은 100자 이내', '각 본문은 220자 이내').replace('본문 카드 5개는 각각', '본문에는')
        prompt += ('\n영상 요약은 한 장입니다. 영상의 구체적 사례나 수치가 확인되면 포함하고, '
                   '우리 업무에서 바로 쓸 수 있는지와 확인이 필요한 한계를 분명히 설명하세요. '
                   '실제 영상에서 확인하지 못한 사실은 만들지 마세요.')
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
    expected_cards = 1 if mode in ('DIRECT_URL', 'LATEST_AI_YOUTUBE') else 5
    schema = copy.deepcopy(SCHEMA)
    schema['properties']['cards']['minItems'] = expected_cards
    schema['properties']['cards']['maxItems'] = expected_cards
    data = generate_with_retry(client, contents, schema, expected_cards)
    for card in data['cards']:
        card['head'] = clean_head(card['head'])
    (OUT / 'digest.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    report_lines = [data['title'], data['subtitle'], '']
    for index, card in enumerate(data['cards'], 1):
        report_lines.extend([f"{index}. {card['head']}", card['desc'], ''])
    report_lines.append('출처')
    report_lines.extend(source['url'] for source in sources)
    (OUT / 'report.txt').write_text('\n'.join(report_lines) + '\n', encoding='utf-8')
    render(data, sources, mode)
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
    print('Created 1 image and ZIP.' if expected_cards == 1 else 'Created 6 cards and ZIP.')

if __name__ == '__main__':
    main()

