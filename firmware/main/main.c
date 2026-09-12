#include <inttypes.h>

#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "m5daylog";

void app_main(void)
{
    ESP_LOGI(TAG, "m5daylog firmware scaffold boot");

    esp_chip_info_t chip_info;
    esp_chip_info(&chip_info);
    ESP_LOGI(TAG, "chip cores: %d, revision: %d", chip_info.cores, chip_info.revision);

    uint32_t flash_size = 0;
    if (esp_flash_get_size(esp_flash_default_chip, &flash_size) == ESP_OK) {
        ESP_LOGI(TAG, "flash size: %" PRIu32 " MB", flash_size / (1024 * 1024));
    } else {
        ESP_LOGW(TAG, "flash size: unknown");
    }

    ESP_LOGI(TAG, "idf version: %s", esp_get_idf_version());
    ESP_LOGI(TAG, "stage: scaffold, result: boot ok");

    // Later Tasks add PDM capture, SD mount, rotation, recovery, and USB.
    // The scaffold keeps running so serial output stays observable.
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        ESP_LOGI(TAG, "stage: scaffold, result: idle");
    }
}
