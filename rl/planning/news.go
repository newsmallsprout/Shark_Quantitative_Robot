package planning

import (
	"context"
	"net/http"
	"strings"
	"time"
)

// NewsIngestor — RSS 新闻抓取 + 风险关键词检测
type NewsIngestor struct {
	client     *http.Client
	lastFetch  int64
	riskWords  []string
}

func NewNewsIngestor() *NewsIngestor {
	return &NewsIngestor{
		client:    &http.Client{Timeout: 15 * time.Second},
		riskWords: []string{
			"hack", "halt", "delist", "SEC", "lawsuit",
			"ban", "crash", "exploit", "rug", "suspension",
			"ETF reject", "emergency", "liquidation cascade",
		},
	}
}

// Fetch 抓取 RSS → 风险标记
func (n *NewsIngestor) Fetch(ctx context.Context) *NewsDigest {
	digest := &NewsDigest{Timestamp: time.Now().Unix()}

	// 尝试多个 RSS 源，任何一个成功即可
	sources := []string{
		"https://cointelegraph.com/rss",
		"https://www.coindesk.com/arc/outboundfeeds/rss/",
	}

	headlines := make(map[string]bool)
	for _, url := range sources {
		lines := n.fetchRSS(ctx, url)
		for _, h := range lines {
			if !headlines[h] {
				headlines[h] = true
				digest.Headlines = append(digest.Headlines, h)

				// 风险关键词检测
				lower := strings.ToLower(h)
				for _, word := range n.riskWords {
					if strings.Contains(lower, strings.ToLower(word)) {
						digest.Flags = append(digest.Flags, word)
					}
				}
			}
		}
	}

	// 去重 flags
	seen := make(map[string]bool)
	unique := digest.Flags[:0]
	for _, f := range digest.Flags {
		if !seen[f] {
			seen[f] = true
			unique = append(unique, f)
		}
	}
	digest.Flags = unique

	// 风险等级
	switch {
	case len(digest.Flags) >= 3:
		digest.RiskLevel = 2
	case len(digest.Flags) >= 1:
		digest.RiskLevel = 1
	default:
		digest.RiskLevel = 0
	}

	n.lastFetch = time.Now().Unix()
	return digest
}

func (n *NewsIngestor) fetchRSS(ctx context.Context, url string) []string {
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil
	}
	req.Header.Set("User-Agent", "Shark/2.0")

	resp, err := n.client.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return nil
	}

	// 简单 XML 解析：提取 <title> 标签内容
	buf := make([]byte, 4096)
	nRead, _ := resp.Body.Read(buf)
	body := string(buf[:nRead])

	var headlines []string
	// 提取所有 <title> 标签（跳过 RSS 频道标题）
	inTitle := false
	titleStart := 0
	for i := 0; i < len(body); i++ {
		if i+7 <= len(body) && body[i:i+7] == "<title>" {
			inTitle = true
			titleStart = i + 7
			i += 6
		} else if inTitle && i+8 <= len(body) && body[i:i+8] == "</title>" {
			title := strings.TrimSpace(body[titleStart:i])
			if title != "" && !strings.Contains(title, "CoinTelegraph") && !strings.Contains(title, "CoinDesk") {
				headlines = append(headlines, title)
			}
			inTitle = false
		}
	}

	return headlines
}
