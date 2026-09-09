# Как загрузить проект в GitHub

## Что специально НЕ входит в GitHub-версию

В репозиторий не следует публиковать:

- `.env` — пароль админки, SECRET_KEY и Telegram-токен;
- `data/shop.db` — база товаров, заказов и персональных данных клиентов;
- `data/media/` — извлечённые из прайсов изображения;
- `data/imports/` — загруженные прайс-листы;
- `*.xlsx` / `*.xlsm` / `*.xls` — прайсы поставщиков.

Все эти пути уже добавлены в `.gitignore`.

## Вариант 1 — Git из терминала

1. Создайте на GitHub новый **пустой** репозиторий (без автосоздания README/.gitignore).
2. Распакуйте этот архив.
3. Откройте терминал в папке проекта.
4. Выполните:

```bash
git init
git add .
git commit -m "Initial HOCO catalog"
git branch -M main
git remote add origin https://github.com/USERNAME/REPOSITORY.git
git push -u origin main
```

Замените `USERNAME/REPOSITORY` на адрес своего репозитория.

## Вариант 2 — GitHub Desktop

1. Распакуйте архив.
2. `File -> Add Local Repository`.
3. Если GitHub Desktop предложит создать репозиторий — согласитесь.
4. Сделайте initial commit.
5. Нажмите `Publish repository`.

## Важно

Не загружайте ZIP как единственный файл в репозиторий: GitHub не распакует его в исходники. Сначала распакуйте архив и коммитьте содержимое папки.
