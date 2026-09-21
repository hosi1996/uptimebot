<?php
/**
 * Iran-side checker for uptimebot. Upload to your Iran host (e.g. /uptimebot/check.php).
 *
 * Request:  POST JSON {"domain": "example.com", "checks": ["dns","https",...]}
 *           header  X-Token: <TOKEN>
 * Response: {"results": {"dns": {"ok": true, "detail": "..."}, ...}}   (ok: true | false | null=unknown)
 *
 * Needs PHP 8.0+ with curl + openssl (standard on shared hosts).
 * "ping" is measured as a TCP connect to port 443/80, since shared hosts rarely allow ICMP.
 */

const TOKEN = 'CHANGE_ME_TO_A_LONG_RANDOM_STRING';   // must match IRAN_CHECK_TOKEN in the bot's .env
const TIMEOUT = 8;
const SLOW_SECONDS = 3;
const SSL_WARN_DAYS = 14;

header('Content-Type: application/json; charset=utf-8');
set_time_limit(60);

function fail_request(int $code, string $message): void
{
    http_response_code($code);
    echo json_encode(['error' => $message]);
    exit;
}

function result(?bool $ok, string $detail): array
{
    return ['ok' => $ok, 'detail' => $detail];
}

function base_domain(string $domain): string
{
    $parts = explode('.', $domain);
    if (count($parts) >= 3 && strlen(end($parts)) === 2 && in_array($parts[count($parts) - 2], ['co', 'com', 'org', 'net', 'gov', 'ac', 'edu'], true)) {
        return implode('.', array_slice($parts, -3));
    }
    return implode('.', array_slice($parts, -2));
}

function dns_values(string $name, int $type, string $key): array
{
    $records = @dns_get_record($name, $type);
    return $records ? array_map(fn($r) => rtrim((string)$r[$key], '.'), $records) : [];
}

function fetch(string $url, bool $follow): array
{
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HEADER => true,
        CURLOPT_FOLLOWLOCATION => $follow,
        CURLOPT_MAXREDIRS => 5,
        CURLOPT_CONNECTTIMEOUT => 5,
        CURLOPT_TIMEOUT => TIMEOUT,
        CURLOPT_USERAGENT => 'UptimeBot/1.0',
    ]);
    $response = curl_exec($ch);
    $info = [
        'error' => $response === false ? (curl_error($ch) ?: 'request failed') : null,
        'code' => (int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE),
        'time' => (float)curl_getinfo($ch, CURLINFO_TOTAL_TIME),
        'location' => (string)curl_getinfo($ch, CURLINFO_REDIRECT_URL),
    ];
    curl_close($ch);
    if ($info['location'] === '' && $response !== false && preg_match('/^location:\s*(\S+)/im', $response, $m)) {
        $info['location'] = $m[1];
    }
    return $info;
}

function check_dns(string $d): array
{
    $ips = array_merge(dns_values($d, DNS_A, 'ip'), dns_values($d, DNS_AAAA, 'ipv6'));
    return $ips ? result(true, implode(', ', $ips)) : result(false, 'no A/AAAA record');
}

function check_ns(string $d): array
{
    $ns = dns_values(base_domain($d), DNS_NS, 'target');
    sort($ns);
    return $ns ? result(true, implode(', ', $ns)) : result(false, 'no NS record');
}

function check_mx(string $d): array
{
    $mx = dns_values($d, DNS_MX, 'target');
    sort($mx);
    return $mx ? result(true, implode(', ', $mx)) : result(false, 'no MX record');
}

function check_ping(string $d): array
{
    foreach ([443, 80] as $port) {
        $start = microtime(true);
        $socket = @fsockopen($d, $port, $errno, $errstr, TIMEOUT);
        if ($socket) {
            fclose($socket);
            return result(true, sprintf('TCP:%d %dms', $port, (microtime(true) - $start) * 1000));
        }
    }
    return result(false, 'no TCP answer on 443/80');
}

function check_http(array $f): array
{
    return $f['error'] ? result(false, $f['error']) : result($f['code'] < 400, 'HTTP ' . $f['code']);
}

function check_redirect(array $f, bool $httpSelected): array
{
    if ($f['error']) {
        return result($httpSelected ? null : false, $f['error']);
    }
    $redirects = in_array($f['code'], [301, 302, 303, 307, 308], true) && strpos($f['location'], 'https://') === 0;
    return $redirects ? result(true, (string)$f['code']) : result(false, 'no redirect to HTTPS (HTTP ' . $f['code'] . ')');
}

function check_speed(array $f, bool $httpsSelected): array
{
    if ($f['error']) {
        return result($httpsSelected ? null : false, $f['error']);
    }
    $ok = $f['time'] <= SLOW_SECONDS;
    return result($ok, sprintf('%.2fs', $f['time']) . ($ok ? '' : ' (slower than ' . SLOW_SECONDS . 's)'));
}

function check_ssl(string $d): array
{
    $context = stream_context_create(['ssl' => [
        'capture_peer_cert' => true,
        'verify_peer' => true,
        'verify_peer_name' => true,
        'peer_name' => $d,
        'SNI_enabled' => true,
    ]]);
    $socket = @stream_socket_client("ssl://$d:443", $errno, $errstr, TIMEOUT, STREAM_CLIENT_CONNECT, $context);
    if (!$socket) {
        return result(false, $errstr ?: 'TLS connection failed');
    }
    $cert = stream_context_get_params($socket)['options']['ssl']['peer_certificate'] ?? null;
    fclose($socket);
    $info = $cert ? openssl_x509_parse($cert) : null;
    if (!$info) {
        return result(null, 'could not read certificate');
    }
    $days = (int)floor(($info['validTo_time_t'] - time()) / 86400);
    return $days < SSL_WARN_DAYS ? result(false, "only $days days left") : result(true, "$days days left");
}

// ---- request handling ----

if (TOKEN === 'CHANGE_ME_TO_A_LONG_RANDOM_STRING') {
    fail_request(500, 'set TOKEN in check.php first');
}
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    fail_request(405, 'POST only');
}
if (!hash_equals(TOKEN, $_SERVER['HTTP_X_TOKEN'] ?? '')) {
    fail_request(403, 'bad token');
}

$body = json_decode(file_get_contents('php://input'), true);
$domain = strtolower(trim($body['domain'] ?? ''));
$names = array_values(array_intersect(
    ['dns', 'ns', 'mx', 'ping', 'http', 'https', 'redirect', 'ssl', 'speed'],
    (array)($body['checks'] ?? [])
));
if (!preg_match('/^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+(xn--[a-z0-9-]+|[a-z]{2,63})$/', $domain)) {
    fail_request(400, 'invalid domain');
}

$http = array_intersect($names, ['http', 'redirect']) ? fetch("http://$domain", false) : null;
$https = array_intersect($names, ['https', 'speed']) ? fetch("https://$domain", true) : null;

$results = [];
foreach ($names as $name) {
    $results[$name] = match ($name) {
        'dns' => check_dns($domain),
        'ns' => check_ns($domain),
        'mx' => check_mx($domain),
        'ping' => check_ping($domain),
        'ssl' => check_ssl($domain),
        'http' => check_http($http),
        'https' => check_http($https),
        'redirect' => check_redirect($http, in_array('http', $names, true)),
        'speed' => check_speed($https, in_array('https', $names, true)),
    };
}
echo json_encode(['results' => $results], JSON_UNESCAPED_UNICODE);
