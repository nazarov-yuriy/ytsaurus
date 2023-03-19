#### **Q: Где взять пример самого простого клиента и пошаговое руководство по запуску MapReduce?**

**A:** Стоит обратиться к разделу Как попробовать, а также прочитать про работу с {{product-name}} из консоли.

------
#### **Q: Всегда ли доступны в операциях номера/названия таблиц?**

**A:** Номера таблиц доступны во всех операциях, за исключением reduce-фазы в [MapReduce](../../../user-guide/data-processing/operations/mapreduce.md). В native C++ wrapper номера таблиц тоже доступны.

------
#### **Q: Если в кластере параллельно работает несколько задач, как распределяются слоты между ними?**

**A:** В {{product-name}} слоты выдаются в соответствии с алгоритмом fair share, и в процессе выполнения задачи число слотов динамически пересчитывается. Подробнее можно прочитать в разделе [Планировщик и пулы](../../../user-guide/data-processing/scheduler/scheduler-and-pools.md).

------
#### **Q: От чего зависят накладные расходы при merge таблицы с ее маленькой дельтой?**

**A:** Накладные расходы зависят от количества затронутых чанков, в которые будут записываться строки из дельты.

------
#### **Q: Можно ли запускать mapper-ы из python interactive shell?**

**A:** Нет, данная функциональность не поддерживается.

------
#### **Q: При чтении таблицы или файла получаю сообщение «Chunk ... is unavailable». Что делать?**
#### **Q: Запущенная на кластере операция остановилась или замедлилась. Появилось сообщение «Some input chunks are not available». Что делать?** { #lostinputchunks }
#### **Q:  Запущенная на кластере операция остановилась или замедлилась. Появилось сообщение «Some intermediate outputs were lost and will be regenerated». Что делать?** { #lostintermediatechunks }

**A:** Часть данных стала недоступна в результате выхода из строя узлов кластера. Недоступность может быть связана с исчезновением реплики с данными (в случае erasure-кодирования), либо с полным исчезновением всех реплик (при отсутствии erasure-кодирования). В любом случае необходимо дождаться восстановления данных, либо починки сломавшихся узлов кластера и принудительно завершить операцию. Следить за состоянием кластера можно в веб-интерфейсе на вкладке System (параметры Lost Chunks, Lost Vital Chunks, Data Missing Chunks, Parity Missing Chunks). Можно досрочно завершить операцию, ожидающую недоступных данных, и получить частичный результат. Для этого необходимо использовать команду `complete-op` в CLI или кнопку **Complete** в веб-интерфейсе на странице операции.

------
#### **Q: Возникла ошибка «Table row is too large: current weight ..., max weight ... или Row weight is too large». Что это значит и как с этим бороться?**

**A:** Вес табличной строки (row weight) считается как сумма длин значений всех колонок в данной строке. В системе установлено ограничение на веса строк, что позволяет контролировать объем буферов используемых при записи таблиц. Ограничение по-умолчанию — 16МБ. Чтобы его увеличить, необходимо выставить опцию `max_row_weight` в конфигурации table_writer.

Длины значений считаются в зависимости от типа:

- `int64`, `uint64`, `double` — 8 байтов;
- `boolean` — 1 байт;
- `string` — длина строки;
- `any` — длина структуры, сериализованной в binary yson, в байтах;
- `null` — 0 байтов.

Если вы получаете данную ошибку при запуске MapReduce-операции, необходимо настроить конфигурацию конкретного table_writer, обслуживающего соответствующую стадию операции: `--spec '{JOB_IO={table_writer={max_row_weight=...}}}'`.

Здесь имя секции `JOB_IO` выбирается следующим образом:

1. для операций с одним типом джобов ([Map](../../../user-guide/data-processing/operations/map.md), [Reduce](../../../user-guide/data-processing/operations/reduce.md), [Merge](../../../user-guide/data-processing/operations/merge.md), и так далее) `JOB_IO = job_io`;
2. для операций Sort `JOB_IO = partition_job_io | sort_job_io | merge_job_io` стоит поднимать все ограничения с шагом x2 до подбора подходящих;
3. для операций [MapReduce](../../../user-guide/data-processing/operations/mapreduce.md) `JOB_IO = map_job_io | sort_job_io | reduce_job_io` можно поднять отдельные ограничения, если вы уверены в том, на какой именно стадии формируются большие строки.

Максимальное значение `max_row_weight` — 128Мб.

------
#### **Q: Возникла ошибка «Key weight is too large». Что это значит и как с этим бороться?**

**A:** Вес ключа в строке (key weight) считается как сумма длин значений всех ключевых колонок в данной строке. Существуют ограничения на вес ключей в строках. Значение лимита по умолчанию 16k. Лимит можно увеличить через опцию `max_key_weight`.

Длины значений считаются в зависимости от типа:

- `int64`, `uint64`, `double` — 8 байтов;
- `boolean` — 1 байт;
- `string` — длина строки;
- `any` — длина структуры, сериализованной в binary yson, в байтах;
- `null` — 0 байтов.

Если вы получаете данную ошибку при запуске MapReduce-операции, то необходимо настроить конфигурацию table_writer, обслуживающего соответствующую стадию операции:

```bash
--spec '{JOB_IO={table_writer={max_key_weight=...}}}'
```

Здесь имя секции `JOB_IO` выбирается следующим образом:

1. для операций с одним типом джобов ([Map](../../../user-guide/data-processing/operations/map.md), [Reduce](../../../user-guide/data-processing/operations/reduce.md), [Merge](../../../user-guide/data-processing/operations/merge.md), etc) `JOB_IO = job_io`;
2. для операций sort `JOB_IO = partition_job_io | sort_job_io | merge_job_io` стоит сразу поднять все ограничения;
3. для операций [MapReduce](../../../user-guide/data-processing/operations/mapreduce.md) `JOB_IO = map_job_io | sort_job_io | reduce_job_io` можно поднять отдельные ограничения, если вы уверены в том, на какой именно стадии формируются большие строки, но лучше также поднять все ограничения.

Максимальное значение `max_key_weight` 256Кб.

{% note warning "Внимание" %}

Граничные ключи чанков хранятся на мастер-серверах, поэтому увеличивать ограничение запрещено за исключением случаев починки продашкена. Перед тем как увеличить ограничение, необходимо написать администратору системы и предупредить об увеличении ограничения, мотивировав свои действия.

{% endnote %}

<!-- ------
#### **Q: При работе джобов, написанных с использованием пакета yandex-yt-python, возникает ошибка «Unicode symbols above 255 are not supported». Что делать?**

**A:** Следует прочитать в разделе [Форматы](../../../user-guide/storage/formats#json) про формат JSON. Можно либо отказаться от использования JSON в пользу [YSON](../../../user-guide/storage/formats#yson), либо указать `encode_utf8=false`. -->

------
#### **Q: Почему долго работает reduce_combiner? Что делать?**

**A:** Вероятно, код джоба достаточно медленный, и стоит уменьшить размер джоба. `reduce_combiner` запускается если размер партиции превышает значение`data_size_per_sort_job`. Объем данных в `reduce_combiner` равен `data_size_per_sort_job`. Значение по умолчанию для `data_size_per_sort_job` задается в конфигурации планировщика {{product-name}}, но может быть переопределено в спецификации операции, в байтах.
`yt map_reduce ... --spec '{data_size_per_sort_job = N}'`

------
#### **Q: Я подаю на вход операции MapReduce несколько входных таблиц и не указываю mapper. При этом в reducer-е я не могу получить индексы входных таблиц. В чем дело?**

**A:** Индексы входных таблиц доступны лишь для mapper-ов. Если mapper не указать, он будет автоматически заменен на тривиальный. Поскольку индекс таблицы не является частью данных, а лишь атрибутами входных строк, то тривиальный mapper не будет сохранять информацию об индексе таблицы. Для решения задачи необходимо самостоятельно написать mapper, в котором сохранить индекс таблицы в каком-либо поле.

------
#### **Q: Как прервать оставшиеся джобы так, чтобы операция не завершилась аварийно?**

**A:** По одному джобу можно прерывать через веб-интерфейс.
Всю операцию целиком можно завершить с помощью CLI `yt complete-op <id>`

------
#### **Q: Что делать, если не работает запуск операций из IPython Notebook?**

**A:** В случае, если текст ошибки похож на следующий:

```python
Traceback (most recent call last):
  File "_py_runner.py", line 113, in <module>
    main()
  File "_py_runner.py", line 40, in main
    ('', 'rb', imp.__dict__[__main_module_type]))
  File "_main_module_9_vpP.py", line 1
    PK
      ^
SyntaxError: invalid syntax
```
Необходимо сделать перед запуском всех операций следующее:

```python
def custom_md5sum(filename):
    with open(filename, mode="rb") as fin:
        h = hashlib.md5()
        h.update("salt")
        for buf in chunk_iter_stream(fin, 1024):
            h.update(buf)
    return h.hexdigest()

yt.wrapper.file_commands.md5sum = custom_md5sum
```

------
#### **Q: Под каким аккаунтом будут храниться промежуточные данные в операциях MapReduce и Sort?**

**A:** По умолчанию используется аккаунт `intermediate`, но такое поведение можно изменить, переопределив параметр `intermediate_data_account` в спецификации операции. Подробнее можно прочитать в разделе [Настройки операций](../../../user-guide/data-processing/operations/operations-options.md).

------
#### **Q: Под каким аккаунтом будут храниться выходные данные операций?**

**A:** Под тем аккаунтом, который окажется на выходных таблицах. Если до запуска операции выходные таблицы не существовали, то будут созданы автоматически, и тогда аккаунт будет унаследован от родительской директории. Для переопределения данных настроек выходные таблицы можно создать заранее и настроить им атрибуты любым образом.

------

#### **Q: Как увеличить количество джобов в операции Reduce? Опция job_count не помогает.**

**A:** Вероятнее всего, входная таблица слишком маленькая, и планировщику не хватает сэмплов ключей, чтобы сформировать большее количество джобов. Чтобы на маленькой таблице получить больше джобов, придется искусственно переложить таблицу, сделав больше чанков. Это можно сделать командой `merge` с помощью опции `desired_chunk_size`. Например, чтобы сделать чанки по 5 мегабайтов, необходимо выполнить команду:

```bash
yt merge --src _table --dst _table --spec '{job_io = {table_writer = {desired_chunk_size = 5000000}}; force_transform = %true}'
```
Альтернативный способ решить проблему — воспользоваться опцией `pivot_keys`, чтобы явно задать граничные ключи, между которыми должны быть запущены джобы.

------
#### **Q: Пытаюсь использовать сортированный выход из операции MapReduce. В качестве ключей на выходе использую ключи на входе. Джобы завершаются аварийно с диагностикой «Output table ... is not sorted: job outputs have overlapping key ranges» или «Sort order violation». В чем дело?**

**A:** Сортированный выход из операции возможен лишь в том случае, если джобы производят непересекающиеся по диапазонам наборы строк. В операции [MapReduce](../../../user-guide/data-processing/operations/mapreduce.md) входные строки группируются по хешу от ключа. Поэтому в описанном сценарии диапазоны из джобов будут пересекаться. Чтобы обойти проблему, нужно использовать комбинацию операций [Sort](../../../user-guide/data-processing/operations/sort.md) и [Reduce](../../../user-guide/data-processing/operations/reduce.md).

------
#### **Q: При запуске операции возникает ошибка «Maximum allowed data weight ... exceeded». Что с этим делать?**

**A:** Ошибка означает, что система сформировала джоб со слишком большим входом — более 200ГБ данных на входе. Такой джоб работал бы слишком долго, поэтому система {{product-name}} сразу защищает пользователей от таких ошибок.

------
#### **Q: При запуске операции Reduce или MapReduce видно, что объем данных, приходящийся на вход reduce джобам, сильно различается. В чем причина и как сделать разбиение более равномерным?**

Причина большого объема может быть в том, что наблюдается перекос во входных данных, то есть каким-то ключам соответствует заметно больший объем данных, чем другим. В таком случае стоит придумать другое решение выполняемой задачи, например попробовать воспользоваться [комбайнерами](../../../user-guide/data-processing/operations/reduce.md#rabota-s-bolshimi-klyuchami-—-reduce_combiner).

Если ошибка возникла в операции [Reduce](../../../user-guide/data-processing/operations/reduce.md), на входе которой более одной таблицы (обычно десятки и сотни), то возможно, планировщику не хватает сэмплов, чтобы разбить входные данные точнее и добиться равномерности. В таком случае стоит использовать операцию [MapReduce](../../../user-guide/data-processing/operations/mapreduce.md) вместо операции [Reduce](../../../user-guide/data-processing/operations/reduce.md).

------
#### **Q: В ходе работы операции Sort или MapReduce появилось сообщение «Intermediate data skew is too high (see "Partitions" tab). Operation is likely to have stragglers». Что делать?** { #intermediatedataskew }

**A:** Это означает, что при партицировании данные оказались разбиты очень неравномерно. Для операции [MapReduce](../../../user-guide/data-processing/operations/mapreduce.md) это с высокой вероятностью означает наличие перекоса во входных данных (каким-то ключам соответствует заметно больший объем данных, чем другим).

Для операции [Sort](../../../user-guide/data-processing/operations/sort.md) такая ситуация тоже возможна, её возникновение связано со спецификой данных и метода сэмплирования. Простого способа решения этой проблемы для [Sort](../../../user-guide/data-processing/operations/sort.md) не существует. Стоит обратиться к администратору системы.

------
#### **Q: Как уменьшить лимит на число падений джобов, начиная с которого вся операция завершается ошибкой?**

**A:** Лимит регулируется настройкой `max_failed_job_count`. Подробнее можно прочитать в разделе [Настройки операций](../../../user-guide/data-processing/operations/operations-options.md).

------
#### **Q: На странице операции появляется оповещение «Average job duration is smaller than 25 seconds, try increasing data_size_per_job in operation spec»?** { #shortjobsduration }

**A:** Сообщение означает, что джобы операции слишком короткие, и накладные расходы на их запуск замедляют операцию и ухудшают утилизацию ресурсов кластера. Для исправления ситуации необходимо увеличить количество данных, подаваемых на вход джобу. Для этого необходимо увеличить соответствующие настройки в спецификации операции:

* **Map, Reduce, JoinReduce, Merge** — `data_size_per_job`;
* **MapReduce**:
  * для `map`/`partition` джобов — `data_size_per_map_job`;
  * для `reduce` джобов — `partition_data_size`;
* **Sort**:
  * для `partition` джобов — `data_size_per_partition_job`;
  * для `final_sort` джобов — `partition_data_size`.

Значения по умолчанию указаны в [разделах](../../../user-guide/data-processing/operations/overview.md), посвящённых конкретным типам операций.

------
#### **Q: На странице операции появляется оповещение «Aborted jobs time ratio ... is is too high. Scheduling is likely to be inefficient. Consider increasing job count to make individual jobs smaller».?** { #longabortedjobs }

**A:** Сообщение означает, что джобы слишком длинные. В результате того, что положенная пулу доля ресурсов на кластере постоянно меняется с приходом и уходом других пользователей, которые запускают операции, джобы операции запускаются, а потом вытесняются. Из-за этого доля времени, потраченного впустую джобами операции, оказывается очень большой. Общая рекомендация состоит в том, чтобы джобы были достаточно короткими. Оптимальная длительность джоба составляет единицы минут. Достичь такого значения можно уменьшением количества данных, подаваемых на вход одному джобу, с помощью опции `data_size_per_job`, либо оптимизацией и ускорением кода.

------
#### **Q: На странице операции появляется оповещение «Average CPU wait time of some of your job types is significantly high...»?** { #highcpuwait }

**A:** Сообщение означает, что джобы существенное время (десятки процентов от общего времени работы джобов) ждали данных от {{product-name}}, или висели на чтение данных с локального диска/по сети. В общем случае это означает, что вы неэффективно утилизируете CPU. В случае ожидания данных от {{product-name}} можно посмотреть в сторону уменьшения `cpu_limit` ваших джобов, либо попробовать переложить данные на SSD, чтобы читать их быстрее. Если это является особенностью вашего процесса, потому что он читает что-то тяжелое с локального диска джоба или ходит куда-то по сети, стоит либо рассмотреть возожность оптимизации процесса, либо также уменьшить `cpu_limit`. Оптимизация подразумевает перестройку процесса пользователя в джобе таким образом, чтобы чтение с диска или обращение по сети не становилось узким местом.

------
#### **Q: Как проще всего выполнить сэмплирование таблицы?**

**A:** В системе {{product-name}} существует возможность заказать семплирование входа для любой операции.
В частности, можно запустить тривиальный map и получить желаемое следующим образом:
`yt map cat --src _path/to/input --dst _path/to/output --spec '{job_io = {table_reader = {sampling_rate = 0.001}}}' --format yson`

Или просто прочитать данные:
`yt read '//path/to/input' --table-reader '{sampling_rate=0.001}' --format json`

------
#### **Q: В ходе работы операции появилось предупреждение «Account limit exceeded» и операция остановилась. Что это значит?** { #operationsuspended }

**A:** Сообщение означает, что в спецификации операции включен параметр `suspend_operation_if_account_limit_exceeded`. Также заполнена одна из квот аккаунта, в котором находятся выходные таблицы операции. Например, квота на дисковое пространство. Следует разобраться почему такое произошло и возобновить операцию. Посмотреть квоты аккаунта можно на странице **Accounts** в веб-интерфейсе.

------
#### **Q: Запущенная операция долгое время находится в состоянии pending. Когда она будет выполняться?** { #operationpending }

**A:** В системе {{product-name}} существует ограничение на количество одновременно **выполняющихся** (в отличие от запущенных, то есть принятых к исполнению) операций в каждом пуле. По умолчанию данное ограничение невелико (порядка 10). В случае, когда в пуле ограничение на число выполняющихся операций оказывается достигнуто, новые операции становятся в очередь. Выполнение операций в очереди будет возможно, когда предыдущие операции в том же пуле завершатся. Ограничение на количество выполняющихся операций действует на всех уровнях иерархии пулов, то есть при запуске операции в пуле A она может попасть в состояние pending, если достигается ограничение не только в самом A, но также хотя бы в одном из родителей A. Подробнее про пулы и их настройку в разделе [Планировщик и пулы](../../../user-guide/data-processing/scheduler/scheduler-and-pools.md). В случае наличия обоснованной необходимости выполнять больше операций одновременно, следует отправить запрос администратору системы.

------
#### **Q: В ходе работы операции появилось предупреждение «Excessive job spec throttling is detected». Что это значит?** { #excessivejobspecthrottling }

**A:** Сообщение означает, что операция тяжелая с точки зрения вычислительных ресурсов, потребляемых самим планировщиком при ее подготовке. Такая ситуация является частью нормального поведения нагруженного кластера. Если вы считаете, что операция выполняется непозволительно долго и продолжительное время находится в состоянии starving, следует сообщить об этом администратору системы.

------
#### **Q: В ходе работы операции появилось предупреждение «Average cpu usage... is lower than requested 'cpu_limit'». Что это значит?** { #lowcpuusage }

**A:** Сообщение означает, что операция потребляет значительно меньше CPU, чем было запрошено. По умолчанию запрашивается одно HyperThreading ядро. Подобная ситуация ведет к тому, что операция занимает больше CPU ресурса, чем потребляет, и CPU-квота пула простаивает. Если такое поведение ожидаемо, то имеет смысл уменьшить cpu_limit для операции (можно задавать дробное число), иначе можно изучить [статистики](../../../user-guide/problems/jobstatistics.md) работы джобов операции, профилировать джоб в процессе работы, чтобы понять, чем он занят.

------
#### **Q: В ходе работы операции появилось предупреждение «Estimated duration of this operation is about ... days». Что это значит?** { #operationtoolong }

**A:** Сообщение означает, что ожидаемое время завершения операции слишком велико. Ожидаемое время завершения рассчитывается на основе оптимистичной оценки завершения бегущих и ожидающих джобов. В силу того, что кластер иногда обновляется, и операции могут запускаться сначала, большое количество потраченных ресурсов могут пропасть зря. Рекомендуется разбивать операции на небольшие или искать способы существенно увеличить квоту, в которой запускается операция.

------
#### **Q: В ходе работы операции появилось предупреждение «Scheduling job in controller of operation <operation_id> timed out». Что это значит?** { #schedulejobtimedout }

**A:** Сообщение означает, что контроллер операции не успевает за отведенное время запустить джоб операции. Такое может происходить, если операция очень тяжелая или планировщик сильно нагружен. Если вы считаете, что операция очень долго выполняется и много находится в starving состоянии, то следует сообщить об этом администратору системы.

------

#### **Q: В ходе работы операции появилось предупреждение «Failed to assign slot index to operation». Что это значит?** { #slotindexcollision }

**A:** Если такое произошло, то обратиться к администратору.

------
#### **Q: В ходе работы операции появилось предупреждение «Operation has jobs that use less than F% of requested tmpfs size» Что это значит?** { #unusedtmpfsspace }

**A:** В спецификации операции заказывается tmpfs для джобов (в атрибутах предупреждения можно узнать, для каких конкретно джобов), но не используется полностью (с некоторыми порогами). Размер tmpfs входит в memory limit, что означает, что джоб запрашивает много памяти, но в итоге ее не использует. Во-первых, это уменьшает реальную утилизацию памяти на кластере. Во-вторых, большие заказы tmpfs могут замедлять планирование джобов, так как найти на кластере слот с 1 Гб памяти гораздо проще, чем, например, слот на 15 Гб памяти. Следует заказывать столько tmpfs, сколько действительно нужно джобам. Реальное использование tmpfs джобами можно узнать в атрибутах предупреждения или посмотрев на [статистику](../../../user-guide/problems/jobstatistics.md) `user_job/tmpfs_size`.

------
#### **Q: При запуске операции получаю ошибку «No online node can satisfy the resource demand», что делать?**

**A:** Сообщение означает, что в кластере нет ни одного подходящего узла для того, чтобы запустить джобы операции. Такое бывает, например, в следующих случаях:

* У операции очень большие требования по CPU или памяти, такие что не хватит ресурсов целиком одного узла кластера. Например, если заказан 1Tb памяти или 1 000 CPU на джоб, то такая операция не отработает и клиент получит ошибку, так как в кластерах {{product-name}} нет узлов с такими характеристиками;
* Указан `scheduling_tag_filter`, под который не подпадает ни один узел кластера.

------
#### **Q: При запуске операции Merge, Reduce возникает ошибка «Maximum allowed data weight violated for a sorted job: xxx > yyy»**

**A:** При формировании джобов по оценке планировщика в один джоб приходит слишком много данных (сотни гигабайт), и у планировщика не получается сделать более мелкий джоб. Возможны следующие варианты:

* При использовании операции [Reduce](../../../user-guide/data-processing/operations/reduce.md), если во входной таблице имеется ключ-монстр - когда одной записи в первой таблице соответствует очень большое число строк в другой таблице, то вследствие гарантий операции [Reduce](../../../user-guide/data-processing/operations/reduce.md) все строки с таким ключом обязаны прийти в один джоб, и такой джоб будет работать бесконечно долго. Стоит использовать [MapReduce](../../../user-guide/data-processing/operations/mapreduce.md) с тривиальным mapper и с reduce combiner, чтобы предобработать ключи-монстры;
* На входе операции много входных таблиц (100 и больше) из-за того, что чанки на границе диапазона учитываются неточно. Общее наблюдение заключается в том, что чем больше входных таблиц, тем меньше пользы от использования сортированного входа. Возможно, стоит воспользоваться операцией [MapReduce](../../../user-guide/data-processing/operations/mapreduce.md);
* При использовании операций [Merge](../../../user-guide/data-processing/operations/merge.md) такая ошибка возможна из-за неоптимальности работы планировщика. Следует обратиться на рассылку `community_ru@ytsaurus.tech`.

Вне зависимости от предыдущих рекомендаций, если вы уверены, что хотите все равно запустить операцию и вы готовы к тому, что она может очень долго работать, можно указать параметр `max_data_weight_per_job`, равный большему значению, и тогда операция запустится.

------
#### **Q: В ходе работы операции появилось предупреждение «Legacy live preview suppressed«, а live preview недоступен. Что это значит?** { #legacylivepreviewsuppressed }

**A:** Live preview - тяжёлый механизм для мастер-серверов, поэтому он по умолчанию отключен для операций, запущенных от роботных пользователей.

Если вы хотите форсировать включение live preview, воспользуйтесь опцией `enable_legacy_live_preview = %true` в спеке операции.

Если хотите отключить данное предупреждение, воспользуйтесь опцией `enable_legacy_live_preview = %false` в спеке операции.
