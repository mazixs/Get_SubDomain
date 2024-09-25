package main

import (
	"bufio"
	"fmt"
	"log"
	"net"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/miekg/dns"
	"github.com/schollz/progressbar/v3"
)

// Функция для загрузки списка из файла
func loadFileToSlice(fileName string) ([]string, error) {
    file, err := os.Open(fileName)
    if err != nil {
        return nil, err
    }
    defer file.Close()

    var lines []string
    scanner := bufio.NewScanner(file)
    for scanner.Scan() {
        line := strings.TrimSpace(scanner.Text())
        if line != "" {
            lines = append(lines, line)
        }
    }
    if err := scanner.Err(); err != nil {
        return nil, err
    }
    return lines, nil
}

// Функция для проверки поддомена на наличие записей A или AAAA
func checkSubdomain(subdomain string, nameservers []string, timeout time.Duration) bool {
    c := new(dns.Client)
    c.Timeout = timeout

    m := new(dns.Msg)
    m.SetQuestion(dns.Fqdn(subdomain), dns.TypeA)

    // Проверяем записи типа A
    for _, ns := range nameservers {
        ns = strings.TrimSpace(ns)
        if ns == "" {
            continue
        }
        nsAddr := net.JoinHostPort(ns, "53")
        r, t, err := c.Exchange(m, nsAddr)
        if err == nil && len(r.Answer) > 0 {
            for _, ans := range r.Answer {
                if _, ok := ans.(*dns.A); ok {
                    return true
                }
            }
        }
        if t >= timeout {
            continue // Пропускаем, если истек таймаут
        }
    }

    // Проверяем записи типа AAAA
    m.SetQuestion(dns.Fqdn(subdomain), dns.TypeAAAA)
    for _, ns := range nameservers {
        ns = strings.TrimSpace(ns)
        if ns == "" {
            continue
        }
        nsAddr := net.JoinHostPort(ns, "53")
        r, t, err := c.Exchange(m, nsAddr)
        if err == nil && len(r.Answer) > 0 {
            for _, ans := range r.Answer {
                if _, ok := ans.(*dns.AAAA); ok {
                    return true
                }
            }
        }
        if t >= timeout {
            continue // Пропускаем, если истек таймаут
        }
    }

    return false
}

// Воркер для обработки поддоменов
func worker(jobs <-chan string, results chan<- string, nameservers []string, timeout time.Duration, progressCh chan<- int) {
    for subdomain := range jobs {
        exists := checkSubdomain(subdomain, nameservers, timeout)
        if exists {
            results <- subdomain
        }
        progressCh <- 1
    }
}

func main() {
    domains, err := loadFileToSlice("domains.txt")
    if err != nil {
        log.Fatalf("Ошибка загрузки доменов: %v", err)
    }

    subdomains, err := loadFileToSlice("subdomains.txt")
    if err != nil {
        log.Fatalf("Ошибка загрузки поддоменов: %v", err)
    }

    nameservers, err := loadFileToSlice("nameservers.txt")
    if err != nil || len(nameservers) == 0 {
        log.Fatalf("Ошибка загрузки DNS-серверов: %v", err)
    }

    outputDir := "results"
    if _, err := os.Stat(outputDir); os.IsNotExist(err) {
        err := os.Mkdir(outputDir, 0755)
        if err != nil {
            log.Fatalf("Не удалось создать директорию %s: %v", outputDir, err)
        }
    }

    timeout := 2 * time.Second
    totalChecks := len(domains) * len(subdomains)
    progressCh := make(chan int, 100)

    bar := progressbar.NewOptions(totalChecks,
        progressbar.OptionSetDescription("Проверка поддоменов..."),
        progressbar.OptionSetWriter(os.Stderr),
        progressbar.OptionShowCount(),
        progressbar.OptionFullWidth(),
    )

    // Горутина для обновления прогрессбара
    go func() {
        for n := range progressCh {
            bar.Add(n)
        }
    }()

    numWorkers := runtime.NumCPU() * 10

    var mainWg sync.WaitGroup
    for _, domain := range domains {
        domain = strings.TrimSpace(domain)
        if domain == "" {
            continue
        }

        mainWg.Add(1)
        go func(domain string) {
            defer mainWg.Done()

            jobs := make(chan string, numWorkers)
            results := make(chan string, numWorkers)
            var wg sync.WaitGroup

            // Запуск воркеров
            for w := 0; w < numWorkers; w++ {
                wg.Add(1)
                go func() {
                    defer wg.Done()
                    worker(jobs, results, nameservers, timeout, progressCh)
                }()
            }

            // Запуск горутины для записи результатов
            outputFile := outputDir + "/" + domain + ".txt"
            resultWg := &sync.WaitGroup{}
            resultWg.Add(1)
            go func() {
                defer resultWg.Done()
                file, err := os.Create(outputFile)
                if err != nil {
                    log.Printf("Ошибка создания файла %s: %v", outputFile, err)
                    return
                }
                defer file.Close()

                for res := range results {
                    fmt.Fprintln(file, res)
                }
            }()

            // Добавление задач в канал jobs
            go func() {
                for _, subdomain := range subdomains {
                    subdomain = strings.TrimSpace(subdomain)
                    if subdomain == "" {
                        continue
                    }
                    fullSubdomain := subdomain + "." + domain
                    jobs <- fullSubdomain
                }
                close(jobs)
            }()

            // Ожидание завершения воркеров
            wg.Wait()
            close(results)
            resultWg.Wait()
        }(domain)
    }

    mainWg.Wait()
    close(progressCh)
    bar.Finish()
    fmt.Println("Проверка завершена.")
}
